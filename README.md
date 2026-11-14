# CloudVault

CloudVault is a file manager built with FastAPI, SQLAlchemy, and vanilla JavaScript. Create an account or open a demo workspace to upload files, organize them into folders, and share download links.

## Features

- Drag and drop files to upload them. Preview images, PDFs, and videos in the browser.
- Create nested folders, search for files, and star items for quick access.
- Move files to the trash and restore them later.
- Create download links that can expire or be revoked.

## Run locally

From the repository root, run these commands in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open [localhost:8000](http://localhost:8000) and select **Explore with a private demo account** to try the file manager.

## Storage

By default, CloudVault stores files on disk and keeps account and file metadata in SQLite. It also supports S3-compatible file storage and PostgreSQL. See `.env.example` for the configuration options.

The included Render configuration uses temporary disk storage. Demo accounts and uploads can disappear after a restart or redeploy.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## License

[MIT](LICENSE)
