# atlan-query

Rebuilds the asset-selection rules of [Atlan](https://atlan.com) data products from one standard template, so that every product in a set of domains selects its assets the same way.

In Atlan, a data product can select its assets **by rule** ("Edit assets" → "Edit via rule"). When those rules are maintained by hand across hundreds of products, they drift apart: typos, leftover conditions, rows pointing at the wrong place, products that match nothing at all. This tool reads each product's current rules, rebuilds them from the template, shows you the difference, and writes the result only when you ask it to.

## How it works

Each data product is identified by a short **entity code**, for example `ABC`. The code is stored in a custom metadata attribute on the assets that belong to the entity, and the product's existing rule already contains it. The tool reads the code from there. The script's messages call this condition the **DEC** (data entity code).

For every product it then builds two rule blocks, joined by **Match any**:

| Block | Matches (all conditions in the block must hold) |
|---|---|
| **1: by table name** | Assets in the landing schemas (by default those starting with `raw_`) that contain tables named `abc_…` or `mnl_abc_…`, whose qualified name contains `/abc_` or `/mnl_abc_`, and whose type is Table, View or Column |
| **2: by entity code** | Assets in **all** schemas of the database, whose entity-code attribute is `ABC`, and whose type is Table, View or Column |

Details:

- **The schemas for block 1 are discovered, not typed in.** The tool searches the Atlan catalog for landing schemas that hold the entity's tables. When new tables appear and Atlan's crawler picks them up, the next run adds their schema automatically.
- **No landing schemas means no block 1.** An empty schema list would mean "all schemas", so the tool leaves block 1 out until a matching schema exists.
- **The leading `/` anchors the name match** to the start of a table name. In a qualified name such as `…/schema/abc_orders/order_id`, the slash comes right before each part. So `/abc_` matches the table `abc_orders` and its columns, but not a column called `xyz_abc_value` or `fabc_id`.
- **Atlan stores every product's rules twice:** once as the rule-builder state that the UI draws, and once as the compiled search query that actually selects the assets. The tool always writes **both**, in a single save, so they stay in sync. It replaces only the rules inside the search query and leaves Atlan's own wrapper around them untouched.
- **It is safe to re-run.** A product that already matches the template is reported as `unchanged` and not written.

## Requirements

- Python 3.11 or newer
- An Atlan API token with permission to read assets and to update data products in the configured domains
- Your data products already select their assets by rule, and each one contains an entity-code condition

## Setup

```bash
git clone <this repository>
cd atlan-query
python -m venv venv
venv\Scripts\activate            # Windows; on macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
```

Create a file called `.env` next to the script (it is git-ignored):

```
ATLAN_BASE_URL=https://<your-tenant>.atlan.com
ATLAN_API_KEY=<your API token>
```

Then copy `settings.example.json` to `settings.json` (also git-ignored) and fill in the values for your Atlan tenant:

| Key | What it is |
|---|---|
| `connection_qualified_name` | Qualified name of the Atlan connection the products point at |
| `database_qualified_name` | Qualified name of the database inside that connection |
| `connection_label` | The connection's display name, as shown in the rule builder |
| `entity_code_attribute_id` | Internal ID of the custom metadata attribute that holds the entity code |
| `entity_code_attribute_label` | That attribute's name, as shown in the rule builder |
| `domain_names` | The Atlan domains whose products the tool processes, spelled exactly as in Atlan |
| `landing_schema_prefix` | Prefix of the schemas block 1 searches (default `raw_`) |
| `table_prefixes` | Table-name prefixes that also belong to an entity: `""` for `abc_…`, `"mnl_"` for `mnl_abc_…` (default `[""]`) |
| `asset_types` | Asset types the rules select (default Table, View, Column) |

The easiest way to find the connection, database and attribute values is to open a product whose rule is already set up correctly and look at its stored rule, the `dataProductAssetsPlaybookFilter` attribute (for example through the pyatlan SDK). The connection's qualified name and label, the database's qualified name, and the entity-code attribute's ID (the row's `key`) and label all appear there.

## Usage

Every run is a **dry run** unless you add `--apply`.

| Command | What it does |
|---|---|
| `python atlan-query.py` | Dry run for every product in the configured domains |
| `python atlan-query.py --domain "Name"` | Dry run for one domain only (repeat `--domain` for several) |
| `python atlan-query.py --only ABC` | Dry run for the product with entity code `ABC` |
| `python atlan-query.py --only ABC --apply` | Write that one product |
| `python atlan-query.py --apply` | Write every product that needs a change |
| `python atlan-query.py --only ABC --force` | Rebuild a product that would otherwise be skipped because its rows point at another connection or have no connection row (see below) |
| `python atlan-query.py --restore backups/ABC_<timestamp>.json` | Put a product's rules back exactly as they were before the tool changed them |

For each product that would change, the output shows the schemas before and after, and how many assets the old and new rules match:

```
ABC  Example Product
  block 1 schemas: raw_sales
               ->  raw_events, raw_sales
  block 2 schemas: raw_sales
               ->  All schemas
  assets matched:  12 -> 340
```

A summary line ends every run:

```
Done: 3 would change, 120 unchanged, 2 skipped, 0 failed.
```

On Windows, save long output to a file with `python atlan-query.py > dryrun.txt`. Note that PowerShell writes that file as UTF-16.

### Recommended workflow

1. Run a dry run for everything, save the output, and read it. Look at the `SKIP` lines, at any product whose asset count drops sharply, and at any count that is suspiciously large.
2. Pick one product and run `--only CODE --apply`. Then open it in Atlan and check that the rules and the asset count look right.
3. Run `--apply` for everything.
4. Run the dry run again. It should report **0 would change**.

## Skipped products

The tool only rewrites products it can read with confidence. It skips the rest, and prints the reason:

| Reason | What to do |
|---|---|
| `the DEC row has 2 values` | The product lists two entity codes. Remove the wrong one, or maintain the product by hand. Empty or blank values are ignored automatically. |
| `it has N blocks; the template has 2` | Remove the extra blocks in the UI, then re-run. |
| `block 1 matches 'x' but the DEC says Y` | The name rule and the entity code disagree, which usually means a typo. Fix whichever one is wrong. |
| `a block points at a different connection` / `has no connection/database/schema row` | Check the product. If its rows are simply wrong, run `--only CODE --force` to rebuild it onto the configured connection. |
| `its search DSL doesn't have the expected shape` | The stored search query isn't laid out the way the tool expects. The tool won't guess, so check the product by hand. |

`--force` only works together with `--only`, so it can never affect more than one product at a time.

## Backups and restoring

Before writing a product, the tool saves its current rules, both copies, to `backups/<CODE>_<timestamp>.json`. To undo a change:

```bash
python atlan-query.py --restore backups/ABC_20260101-120000-000000.json
```

## Adding a new data product

The tool reads a product's entity code from its existing rule, so a brand-new product needs one seed condition:

1. Make sure the new entity code exists as a value of the entity-code attribute.
2. Create the data product in one of the configured domains.
3. Under "Edit assets" → "Edit via rule", add a single condition: *entity code is `ABC`*. Save.
4. Run `python atlan-query.py --only ABC`, check the output, then add `--apply`.

If the entity's tables don't exist yet, the product gets only block 2. Once they land and Atlan has crawled them, the next run adds block 1.

## Limitations

- One entity code per product.
- One connection and one database.
- Block 2 only finds assets that carry the entity code. Tagging assets with the code is outside the scope of this tool.
- Atlan's asset counts are approximate, so small differences from a direct database count are normal.
