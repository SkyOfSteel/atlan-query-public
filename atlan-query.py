"""
atlan-query.py

Rebuilds the asset-selection rules ("Edit assets" > "Edit via rule") of the
Atlan data products in the domains listed in settings.json, so that every
product follows the same template:

  Block 1 (Match all): the landing schemas that hold the entity's tables,
                       Qualified name contains /<prefix><code>_ for each table
                       prefix, Asset type is one of the configured types.
                       Only when such schemas exist: an empty schema list would
                       mean All schemas, so without them there is no block 1
                       (the next run adds it once a schema appears).
  Block 2 (Match all): All schemas of the database, entity code is <CODE>,
                       Asset type is one of the configured types.
  The two blocks are joined by Match any.

The slash in "/<code>_" anchors the match to the start of a table name, so a
table called abc_orders matches ABC, but a column called fabc_id does not.

Products whose rules don't fit (Match all at the top, a missing block, a stray
underscore) are rebuilt from the template. Products the template can't express
(two entity codes, three or more blocks, another connection) are skipped and listed.

Atlan stores a product's rules twice: the rule-builder filter the UI draws, and
the search DSL that actually selects assets. This script writes both, so they
stay in sync. Landing schemas are found with an Atlan search, so re-running the
script picks up new schemas after Atlan's next crawl.

Usage:
  python atlan-query.py                        dry run for all products (writes nothing)
  python atlan-query.py --domain "Domain A"    dry run for one domain (repeat --domain for several)
  python atlan-query.py --only ABC             dry run for one product
  python atlan-query.py --only ABC --apply     write one product (a backup is saved first)
  python atlan-query.py --apply                write every product that needs a change
  python atlan-query.py --restore backups/ABC_20260101-120000-000000.json
  python atlan-query.py --only ABC --force     rebuild a product the script would skip because
                                               its rows point at another connection or have no
                                               connection row (only after checking it by hand)

Needs ATLAN_BASE_URL and ATLAN_API_KEY in .env, and the values specific to your
Atlan tenant in settings.json (copy settings.example.json to start).
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pyatlan.client.atlan import AtlanClient
from pyatlan.model.assets import DataDomain, DataProduct, Table, View
from pyatlan.model.fluent_search import CompoundQuery, FluentSearch
from pyatlan.model.search import IndexSearchRequest, Term

load_dotenv()

SETTINGS_FILE = Path(__file__).with_name("settings.json")
REQUIRED_SETTINGS = ["connection_qualified_name", "database_qualified_name", "connection_label",
                     "entity_code_attribute_id", "entity_code_attribute_label", "domain_names"]


def load_settings():
    """Read settings.json, which holds the values specific to your Atlan tenant (it is git-ignored)."""
    if not SETTINGS_FILE.exists():
        raise SystemExit(f"{SETTINGS_FILE.name} not found: copy settings.example.json to {SETTINGS_FILE.name} and fill it in.")
    settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED_SETTINGS if key not in settings]
    if missing:
        raise SystemExit(f"{SETTINGS_FILE.name} is missing: {', '.join(missing)}")
    return settings


SETTINGS = load_settings()
CONNECTION_QN = SETTINGS["connection_qualified_name"]
DATABASE_QN = SETTINGS["database_qualified_name"]
CONNECTION_LABEL = SETTINGS["connection_label"]
DEC_KEY = SETTINGS["entity_code_attribute_id"]          # the custom metadata attribute that holds the entity code
DEC_LABEL = SETTINGS["entity_code_attribute_label"]     # its name as the rule builder shows it
DOMAIN_NAMES = SETTINGS["domain_names"]                 # exact names as in Atlan
LANDING_SCHEMA_PREFIX = SETTINGS.get("landing_schema_prefix", "raw_")
TABLE_PREFIXES = SETTINGS.get("table_prefixes", [""])   # an entity's tables are named <prefix><code>_..., e.g. abc_x or mnl_abc_x
ASSET_TYPES = SETTINGS.get("asset_types", ["Table", "View", "Column"])
BACKUP_DIR = Path("backups")


# ---------------------------------------------------------------- the filter

def hierarchy_row(schema_qns):
    """Build the connection/database/schema row from a list of schema qualified names."""

    if schema_qns:
        attribute_name = "schemaQualifiedName"
        attribute_value = ";".join([DATABASE_QN] + schema_qns)

    else:
        attribute_name = "databaseQualifiedName"
        attribute_value = DATABASE_QN

    return {"key": "hierarchy",
            "operator": "eq",
            "value":
            {"connectionQualifiedName": CONNECTION_QN,
             "attributeValue": attribute_value,
             "attributeName": attribute_name},
             "isLocked": False,
             "isMuted": False,
             "label": CONNECTION_LABEL}

def name_patterns(code):
    """Return the table-name starts the name rule looks for, e.g. ['/abc_', '/mnl_abc_']."""
    patterns = []
    for prefix in TABLE_PREFIXES:
        patterns.append("/" + prefix + code.lower() + "_")
    return patterns

def name_row(code):
    """Build the 'Qualified name contains /<code>_' row; the slash means "a table name starting with"."""

    return {'key': 'qualifiedName',
            'operator': 'contains',
            'value': name_patterns(code),
            'isLocked': False,
            'isMuted': False,
            'label': 'Qualified name'}

def type_row():
    """Build the 'Asset type is Table, View or Column' row."""
    return {'key': '__typeName.keyword',
            'operator': 'eq',
            'value': ASSET_TYPES,
            'isLocked': False,
            'isMuted': False,
            'label': 'Asset type'}

def dec_row(code):
    """Build the 'entity code is <CODE>' row (the DEC row in the script's messages)."""
    return {'key': DEC_KEY,
            'operator': 'eqAnd',
            'value': [code.upper()],
            'isLocked': False,
            'isMuted': False,
            'label': DEC_LABEL}

def group(condition, rules):
    """Wrap rows (or blocks) in a Match all ("AND") or Match any ("OR") group."""
    return {"condition": condition, "isGroupLocked": False, "rules": rules}

def build_filter(code, schema_qns):
    """Build a product's full rule filter from its code and its raw schemas; block 2 is always All schemas."""
    block_1 = group("AND", [hierarchy_row(schema_qns), name_row(code), type_row()])
    block_2 = group("AND", [hierarchy_row([]), dec_row(code), type_row()])
    if not schema_qns:                    # no raw schemas: block 1 would mean All schemas, so leave it out
        return group("OR", [block_2])
    return group("OR", [block_1, block_2])


# ---------------------------------------------------------------- reading the current filter

class SkipProduct(Exception):
    """Raised when a product doesn't match the expected pattern, so the script leaves it alone."""


def find_row(block, keys):
    """Return the first row in a block whose key is one of `keys`, or None."""
    for row in block.get("rules", []):
        if row.get("key") in keys:
            return row
    return None


def hierarchy_of(block):
    """Return a block's connection/database/schema row; skip the product if there is none."""
    row = find_row(block, ["hierarchy"])
    if row is None:
        raise SkipProduct("a block has no connection/database/schema row")
    return row


def listed_schemas(row):
    """Return the schema QNs a hierarchy row lists; an empty list means All schemas."""
    value = row.get("value", {})
    if value.get("connectionQualifiedName") != CONNECTION_QN:
        raise SkipProduct("a block points at a different connection")
    if value.get("attributeName") == "databaseQualifiedName":
        if value.get("attributeValue") != DATABASE_QN:
            raise SkipProduct("a block points at a different database")
        return []
    schemas = []
    for qn in value.get("attributeValue", "").split(";"):
        if qn == DATABASE_QN:
            continue                      # the UI always puts the database first; skip it
        if not qn.startswith(DATABASE_QN + "/"):
            raise SkipProduct(f"a block lists a schema in another database: {qn}")
        schemas.append(qn)
    return schemas


MISSING_BLOCK = {"rules": []}             # stands in for a block the product doesn't have yet


def split_blocks(current_filter):
    """Return (block 1, block 2) of a current filter; block 2 is the one with the DEC row."""
    blocks = current_filter.get("rules", [])   # Match all or Match any at the top: the template writes Match any
    if not blocks:
        raise SkipProduct("it has no rule blocks")
    if len(blocks) > 2:
        raise SkipProduct(f"it has {len(blocks)} blocks; the template has 2")
    if len(blocks) == 1:                       # the template adds the missing block
        if find_row(blocks[0], [DEC_KEY]):
            return MISSING_BLOCK, blocks[0]
        return blocks[0], MISSING_BLOCK
    first, second = blocks
    if find_row(second, [DEC_KEY]):
        return first, second
    if find_row(first, [DEC_KEY]):
        return second, first
    raise SkipProduct("neither of its 2 blocks has a DEC row")


def name_rule_value(block_1):
    """Return the value of block 1's 'contains' or 'starts with' name rule (e.g. '/abc_'), or None."""
    for row in block_1.get("rules", []):
        is_name_rule = row.get("key") in ["qualifiedName", "name"] and row.get("operator") in ["contains", "startsWith"]
        if is_name_rule and row.get("value"):
            return row["value"][0]
    return None


def as_code(text):
    """Turn a rule value like '/abc_', 'abc_' or a typo like '_abc' into a code like 'ABC'."""
    return text.strip().strip("/_").upper()


def product_code(block_1, block_2):
    """Read the product's code from its DEC row (or its name rule if it has none), cross-checking the two."""
    name_value = name_rule_value(block_1)
    dec = find_row(block_2, [DEC_KEY])
    if dec is not None:
        values = [value for value in dec.get("value", []) if isinstance(value, str) and value.strip()]   # an empty or blank chip is a typo, not a code
        if len(values) != 1:
            raise SkipProduct(f"the DEC row has {len(values)} values, expected 1")
        code = values[0].strip().upper()
        if name_value is not None and as_code(name_value) != code:
            raise SkipProduct(f"block 1 matches {name_value!r} but the DEC says {code}")
    elif name_value is not None:
        code = as_code(name_value)
    else:
        raise SkipProduct("it has neither a DEC row nor a name rule")
    if len(code) != 3 or not code.isalnum():
        raise SkipProduct(f"{code!r} is not a three-character code")
    return code


def current_schemas(block):
    """Return the schemas a block lists ([] means All schemas), or None if the product has no such block."""
    if block is MISSING_BLOCK:
        return None
    return listed_schemas(hierarchy_of(block))



# ---------------------------------------------------------------- the DSL (same rules, search language)

def dsl_wildcard(pattern):
    """Build a case-insensitive wildcard on qualifiedName, the way the UI writes it."""
    return {"wildcard": {"qualifiedName": {"case_insensitive": True, "value": pattern}}}


def dsl_hierarchy(schema_qns):
    """DSL version of hierarchy_row: one term per schema, or a database prefix for All schemas."""
    if schema_qns:
        terms = []
        for qn in [DATABASE_QN] + schema_qns:   # the UI includes the database too; we copy that
            terms.append({"term": {"schemaQualifiedName": qn}})
        return {"bool": {"should": terms}}
    return {"bool": {"should": [dsl_wildcard(DATABASE_QN + "*")]}}


def dsl_name(code):
    """DSL version of name_row: qualified name contains any of the name patterns."""
    wildcards = []
    for pattern in name_patterns(code):
        wildcards.append(dsl_wildcard("*" + pattern + "*"))
    return {"bool": {"should": wildcards}}


def dsl_dec(code):
    """DSL version of dec_row: entity code is <CODE>."""
    return {"bool": {"filter": {"term": {DEC_KEY: code.upper()}}}}


def dsl_type():
    """DSL version of type_row: asset type is Table, View or Column."""
    return {"terms": {"__typeName.keyword": ASSET_TYPES}}


def build_dsl_rules(code, schema_qns):
    """DSL version of build_filter: the part of the search query that holds the two blocks."""
    block_1 = {"bool": {"must": [dsl_hierarchy(schema_qns), dsl_name(code), dsl_type()]}}
    block_2 = {"bool": {"must": [dsl_hierarchy([]), dsl_dec(code), dsl_type()]}}
    if not schema_qns:                    # same rule as build_filter: no raw schemas, no block 1
        return {"bool": {"should": [block_2]}}
    return {"bool": {"should": [block_1, block_2]}}


def rules_slot(dsl):
    """Return the DSL list whose first item holds the rules (the rest is Atlan's own wrapper)."""
    try:
        must = dsl["query"]["dsl"]["query"]["bool"]["filter"]["bool"]["must"]
    except (KeyError, TypeError):
        raise SkipProduct("its search DSL doesn't have the expected shape")
    if len(must) != 2 or "bool" not in must[0] or must[1] != {"term": {"__state": "ACTIVE"}}:
        raise SkipProduct("its search DSL doesn't have the expected shape")
    return must


# ---------------------------------------------------------------- talking to Atlan

def find_domain_qn(client, domain_name):
    """Look up a domain's qualified name by its display name; it must be unique."""
    request = (
        FluentSearch()
        .where(CompoundQuery.active_assets())
        .where(CompoundQuery.asset_type(DataDomain))
        .where(DataDomain.NAME.eq(domain_name))
        .to_request()
    )
    found = []
    for domain in client.asset.search(request):
        found.append(domain.qualified_name)
    if len(found) != 1:
        raise RuntimeError(f"Expected exactly one domain named {domain_name!r}, found {len(found)}: {found}")
    return found[0]


def find_products(client, domain_names):
    """Return every data product directly inside the given domains."""
    products = []
    for domain_name in domain_names:
        request = (
            FluentSearch()
            .where(CompoundQuery.active_assets())
            .where(CompoundQuery.asset_type(DataProduct))
            .where(Term(field="parentDomainQualifiedName", value=find_domain_qn(client, domain_name)))
            .include_on_results("dataProductAssetsPlaybookFilter")
            .include_on_results("dataProductAssetsDSL")
            .to_request()
        )
        for product in client.asset.search(request):
            products.append(product)
    return products


def find_raw_schemas(client, code):
    """Find the landing schemas that hold tables or views named <prefix><code>_..., sorted."""
    schemas = set()                       # a set keeps each schema once, however many tables it has
    for prefix in TABLE_PREFIXES:
        request = (
            FluentSearch()
            .where(CompoundQuery.active_assets())
            .where(CompoundQuery.asset_types([Table, View]))
            .where(Table.DATABASE_QUALIFIED_NAME.eq(DATABASE_QN))
            .where(Table.SCHEMA_NAME.startswith(LANDING_SCHEMA_PREFIX))
            .where(Table.NAME.startswith(prefix + code.lower() + "_"))
            .include_on_results(Table.SCHEMA_QUALIFIED_NAME)
            .to_request()
        )
        for asset in client.asset.search(request):
            schemas.add(asset.schema_qualified_name)
    return sorted(schemas)                # same order every run, so unchanged products stay unchanged


def count_assets(client, dsl):
    """Ask Atlan how many assets a DSL selects; best effort, returns '?' if it can't."""
    try:
        return client.asset.search(IndexSearchRequest(**dsl["query"])).count
    except Exception:
        return "?"


def backup(product, code):
    """Save a product's current rules to backups/<CODE>_<timestamp>.json before changing them."""
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"{code}_{datetime.now():%Y%m%d-%H%M%S-%f}.json"   # microseconds: two products can share a code
    saved = {
        "guid": product.guid,
        "name": product.name,
        "qualified_name": product.qualified_name,
        "filter": product.data_product_assets_playbook_filter,
        "dsl": product.data_product_assets_d_s_l,
    }
    path.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return path


def save_rules(client, qualified_name, name, filter_text, dsl_text):
    """Write both copies of a product's rules to Atlan in one save."""
    product = DataProduct.updater(qualified_name=qualified_name, name=name)
    product.data_product_assets_playbook_filter = filter_text
    product.data_product_assets_d_s_l = dsl_text
    client.asset.save(product)


def restore(client, path):
    """Put a product's rules back exactly as they were in a backup file."""
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    save_rules(client, saved["qualified_name"], saved["name"], saved["filter"], saved["dsl"])
    print(f"Restored {saved['name']} from {path}")


# ---------------------------------------------------------------- one product, then all of them

def describe(schema_qns):
    """Turn a list of schema QNs into short names for printing; empty means All schemas."""
    if schema_qns is None:
        return "(no such block)"
    if not schema_qns:
        return "All schemas"
    names = []
    for qn in schema_qns:
        names.append(qn.split("/")[-1])
    return ", ".join(names)


def process_product(client, product, only, apply, force=False):
    """Rebuild one product's rules, print what would change, and write them if apply is True."""
    if not product.data_product_assets_playbook_filter or not product.data_product_assets_d_s_l:
        raise SkipProduct("it has no rules stored")
    old_filter = json.loads(product.data_product_assets_playbook_filter)
    block_1, block_2 = split_blocks(old_filter)
    code = product_code(block_1, block_2)
    if only and code != only.upper():
        return "filtered"

    forced_past = None
    try:
        old_block_1 = current_schemas(block_1)
        old_block_2 = current_schemas(block_2)
    except SkipProduct as reason:
        if not force:
            raise
        old_block_1 = old_block_2 = None      # --force: the old rows are only for printing, so carry on
        forced_past = reason
    raw_schemas = find_raw_schemas(client, code)

    new_filter = build_filter(code, raw_schemas)
    new_rules = build_dsl_rules(code, raw_schemas)
    old_dsl = json.loads(product.data_product_assets_d_s_l)
    new_dsl = json.loads(product.data_product_assets_d_s_l)   # a second copy, the one we change
    must = rules_slot(new_dsl)

    if new_filter == old_filter and new_rules == must[0]:
        print(f"{code}  {product.name}: unchanged")
        return "unchanged"

    must[0] = new_rules                   # swap in our rules; Atlan's wrapper around them stays as it was
    print(f"{code}  {product.name}")
    if forced_past:
        print(f"  --force: rebuilt although {forced_past}")
    print(f"  block 1 schemas: {'(not read)' if forced_past else describe(old_block_1)}")
    print(f"               ->  {describe(raw_schemas or None)}")   # no raw schemas: no block 1
    print(f"  block 2 schemas: {'(not read)' if forced_past else describe(old_block_2)}")
    print("               ->  All schemas")                        # block 2 is always All schemas
    print(f"  assets matched:  {count_assets(client, old_dsl)} -> {count_assets(client, new_dsl)}")

    if apply:
        path = backup(product, code)
        filter_text = json.dumps(new_filter, separators=(",", ":"))
        dsl_text = json.dumps(new_dsl, separators=(",", ":"))
        save_rules(client, product.qualified_name, product.name, filter_text, dsl_text)
        print(f"  WRITTEN (old rules saved to {path})")
    return "changed"


def main():
    """Rebuild the rules of the products in the configured domains: a dry run unless --apply is given."""
    parser = argparse.ArgumentParser(description="Rebuild data product asset rules in Atlan from a template.")
    parser.add_argument("--domain", action="append", metavar="NAME",
                        help="process only this domain (repeat for several); default: every domain in DOMAIN_NAMES")
    parser.add_argument("--only", metavar="CODE", help="process just one product, e.g. --only ABC")
    parser.add_argument("--apply", action="store_true", help="really write the changes (default: dry run)")
    parser.add_argument("--restore", metavar="BACKUP_FILE", help="put one product back from a backup file")
    parser.add_argument("--force", action="store_true",
                        help="with --only: rebuild even if its rows point at another connection or have no connection row")
    args = parser.parse_args()
    if args.force and not args.only:
        parser.error("--force only works together with --only, so it can't touch more than one product")

    client = AtlanClient()

    if args.restore:
        restore(client, args.restore)
        return

    counts = {"changed": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    for product in find_products(client, args.domain or DOMAIN_NAMES):
        try:
            status = process_product(client, product, args.only, args.apply, args.force)
        except SkipProduct as reason:
            if not args.only:
                print(f"SKIP  {product.name}: {reason}")
                counts["skipped"] += 1
            continue
        except Exception as error:
            print(f"FAILED  {product.name}: {error}")
            counts["failed"] += 1
            continue
        if status != "filtered":
            counts[status] += 1

    if args.only and counts["changed"] + counts["unchanged"] + counts["failed"] == 0:
        print(f"No product with code {args.only.upper()} was processed. "
              "Run without --only to see whether it was skipped, and why.")
    changed_word = "changed" if args.apply else "would change"
    print(f"\nDone: {counts['changed']} {changed_word}, {counts['unchanged']} unchanged, "
          f"{counts['skipped']} skipped, {counts['failed']} failed.")
    if not args.apply:
        print("Dry run: nothing was written. Add --apply to write.")


if __name__ == "__main__":
    main()