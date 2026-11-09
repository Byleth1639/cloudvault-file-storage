# CloudVault: start here

A working file storage web app built as a resume/portfolio project.

## Option A — Run it on your computer

1. Unzip this project.
2. Open **PowerShell** inside the extracted `cloudvault` folder.
3. Run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

4. Open **http://localhost:8000**.
5. Click **Explore with a private demo account**.
6. Create a folder and upload a test file. Open the three-dot menu to download, share, star, move, rename, or trash it.
7. To stop the app, press **Ctrl+C** in PowerShell.

If PowerShell blocks activation scripts, you can skip activation and use:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

## Option B — Put it on GitHub

Create an **empty** GitHub repository named `cloudvault-file-storage`, then in PowerShell inside `cloudvault` run:

```powershell
git init
git add .
git commit -m "Build CloudVault file storage platform"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/cloudvault-file-storage.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your actual username. If you have existing remotes, use `git remote set-url origin <URL>`.

## Option C — Free Render **portfolio demo**

- Render Dashboard → **New → Web Service** → choose your new GitHub repository.
- Runtime: **Docker**; Dockerfile path: `./Dockerfile`; root directory blank; branch `main`.
- Choose the Free plan only if offered. Set environment variable `COOKIE_SECURE=true`.
- Click Deploy. Visit the URL and test uploading a file.

**Read this before using Render:** demo-mode SQLite and stored files are on an ephemeral disk and can disappear after any restart or redeploy. Do not store important documents. The project is not production-hardened. If you want persistence, read `README.md` for PostgreSQL and S3-compatible storage configuration.
