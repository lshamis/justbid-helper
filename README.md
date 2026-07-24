# 🪄 JustBid Helper

Automatically filters JustBid.com listings to highlight **"Appears New"** condition items in green, and overlay other conditions in red to make browsing easier.

This repository is optimized to run as a **GitHub Page** so that anyone (including non-technical friends) can install the helper script with just a single click.

---

## 🚀 How to Set Up Your GitHub Repository & Page

To share this helper with your friends easily, follow these steps to upload it to GitHub and enable GitHub Pages:

### Step 1: Initialize Git and Commit Your Files
Open your terminal in this directory and run:
```bash
# Initialize git repository
git init -b main

# Add files
git add index.html justbid-helper.user.js README.md

# Create your first commit
git commit -m "Initial commit: JustBid Helper"
```

### Step 2: Create a New GitHub Repository
1. Go to [GitHub](https://github.com/) and click **New** repository.
2. Name the repository: `justbid-helper`
3. Leave it **Public** (required for GitHub Pages).
4. Do **not** initialize it with a README, `.gitignore`, or license.
5. Click **Create repository**.

### Step 3: Link Your Repository and Push
Copy the push commands from GitHub or run:
```bash
# Link your local repo to your GitHub repo (replace your-username with your actual GitHub username)
git remote add origin https://github.com/your-username/justbid-helper.git

# Push to GitHub
git push -u origin main
```

### Step 4: Turn on GitHub Pages
1. On your GitHub repository page, click the **Settings** tab.
2. In the left sidebar, click on **Pages** (under the "Code and automation" section).
3. Under **Build and deployment** -> **Source**, select **Deploy from a branch**.
4. Under **Branch**, select `main` and keep `/ (root)`, then click **Save**.
5. After 1–2 minutes, GitHub will give you a public URL (e.g., `https://your-username.github.io/justbid-helper/`).

---

## 💻 How to Install

1. **Install Violentmonkey**: Click the link for your browser (Chrome/Firefox/Edge) on your public page to add the Violentmonkey extension.
2. **Install JustBid Helper**: Click the **"Click Here to Install the JustBid Helper Script"** button. Violentmonkey will automatically detect the script, open an installation tab, and you just need to click **"Confirm Installation"**.
3. **Start Browsing**: Open [JustBid.com](https://www.justbid.com) and the helper will automatically start filtering listings!

---

## 🔄 How Updates Work (Auto-Updates)

When you make a change to `justbid-helper.user.js` and push it to your GitHub repository, Violentmonkey will automatically check for updates on your friend's browser and update it for them. They won't have to download or configure anything again!

---

## 🛠️ Files in this Repository

- [justbid-helper.user.js](file:///home/lshamis/justbid-filter/justbid-helper.user.js): The main userscript that runs in Violentmonkey.
- [index.html](file:///home/lshamis/justbid-filter/index.html): The simple, 90s-style landing page.
- [README.md](file:///home/lshamis/justbid-filter/README.md): This documentation file.
