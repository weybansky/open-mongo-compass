# Open Mongo Compass

A small, read-only MongoDB browser for machines where you can't install MongoDB Compass but can run Python. It works with MongoDB and Azure Cosmos DB for MongoDB.

It has a FastAPI backend and a single HTML file with no build step, no npm and no CDN assets.

## Run

macOS or Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Windows:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```

Then open <http://127.0.0.1:8765> in a browser on the same machine. To use another port, set the `PORT` environment variable.

## Use

1. Paste your connection string and click **Connect**. For Cosmos DB, copy it from **Connection strings** on the account in the Azure portal.
2. Expand a database and click a collection.
3. Enter a filter, projection or sort, then click **Find** or press Enter.

Filters accept shell-style syntax as well as JSON:

```js
{ status: "active" }
{ name: /smith/i, age: { $gte: 21 } }
{ _id: ObjectId("65f000000000000000000000") }
{ createdAt: { $gte: ISODate("2026-01-01") } }
```

You can switch between list and table views, page through results, copy a document, or download the current page as JSON. Each page holds up to 1000 documents.

**Count matches** only runs when you click it, because counting a large collection can use a lot of Cosmos DB request units. With an empty filter it shows a fast estimate.

## Security

- The app is read-only: its only database operations are ping, list, find and count.
- The server only listens on `127.0.0.1` and has no login, so don't expose it or put it behind a proxy.
- Connection strings stay in server memory until you disconnect or stop the app. They are only written to disk if you tick **Save in this browser**, which stores them unencrypted in that browser's local storage.

## Troubleshooting

- **Connection times out:** check that this machine can reach the database, including any firewall or private endpoint rules.
- **TLS certificate errors:** add your company's CA to the connection string, for example `&tlsCAFile=C:/certs/company-ca.pem`.
- **No databases are listed:** your user may not have permission to list them. Add the database name to the connection string, as in `mongodb://host:27017/mydb`.
- **Error 16500 (Cosmos DB):** the account ran out of request units. Try a smaller limit or a more specific filter on an indexed field.
- **`pip install` can't reach PyPI:** run `pip download -r requirements.txt -d wheels` on a machine with the same OS and Python version, copy `wheels/` across, then run `pip install --no-index --find-links wheels -r requirements.txt`.
