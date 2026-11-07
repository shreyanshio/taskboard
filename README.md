<div align="center">

# 📋 TaskBoard — Daily Tracker

### Daily tracker. Visual progress. Zero excuses.

A clean, single-page productivity dashboard to plan your day, track tasks, and stay consistent — with a built-in study timer, analytics, and a global leaderboard. 

🔗 **[Live Demo →  taskboard7.up.railway.app](https://taskboard7.up.railway.app/)**

![HTML5](https://img.shields.io/badge/HTML5-E34F26?style=for-the-badge&logo=html5&logoColor=white)
![CSS3](https://img.shields.io/badge/CSS3-1572B6?style=for-the-badge&logo=css3&logoColor=white)
![JavaScript](https://img.shields.io/badge/JavaScript-F7DF1E?style=for-the-badge&logo=javascript&logoColor=black)
![Supabase](https://img.shields.io/badge/Supabase-3ECF8E?style=for-the-badge&logo=supabase&logoColor=white)
![Railway](https://img.shields.io/badge/Deployed%20on-Railway-0B0D0E?style=for-the-badge&logo=railway&logoColor=white)

</div>

---

## 📖 Table of Contents

- [✨ Features](#-features)
- [🖥️ Pages & Navigation](#️-pages--navigation)
- [🛠️ Tech Stack](#️-tech-stack)
- [📂 Project Structure](#-project-structure)
- [🚀 Running Locally](#-running-locally)
- [🔌 Backend Requirement](#-backend-requirement)
- [🙌 Author](#-author)
- [📄 License](#-license)

---

## ✨ Features

| | Feature | Description |
|---|---|---|
| 📋 | **Task Board** | Add, edit, complete, pin & organize tasks by category and priority, with subtasks |
| 📅 | **Calendar View** | Click any day to see what's on your plate |
| 📚 | **Study Timer** | Track focused study time per subject — auto-resets at midnight |
| 🍅 | **Pomodoro Focus** | Built-in Pomodoro mode with quick presets (25 min → 3 hrs) |
| 📊 | **Graphs** | Daily completions, weekly trends, and a 30-day activity heatmap |
| 🔥 | **Analytics & Streak** | Track your day streak, category breakdowns, and 4-week trends |
| 💡 | **Smart Insights** | Auto-generated highlights — subject breakdown, focus distribution, monthly study heatmap |
| 🏆 | **Global Leaderboard** | See how your study hours & focus stack up against the community |
| 👤 | **Profile & Achievements** | XP levels, achievement badges, and profile customization |
| 🔐 | **Flexible Sign-in** | Email/password, Google OAuth, or "Continue with Telegram" |
| 🌙 | **Dark Mode** | Because late-night study sessions deserve better on the eyes |

---

## 🖥️ Pages & Navigation

```
📋 Tasks        → Your daily task board
📅 Calendar     → Monthly view of all tasks
📊 Graphs       → Visual progress charts
🔥 Analytics    → Streaks, categories & trends
🏆 Leaderboard  → Community rankings
💡 Insights     → Deep dive into study patterns
👤 Profile      → Settings, achievements & account
```

---

## 🛠️ Tech Stack

- **Frontend:** Vanilla HTML5 + CSS3 + JavaScript — a single file, zero frameworks, zero build step
- **Backend:** A FastAPI + Supabase REST API (deployed separately), talked to via a lightweight fetch-based client built into the page
- **Hosting:** [Railway](https://railway.app/)

---

## 📂 Project Structure

```
├── 📄 index.html   # Markup only — auth screens, task board, timer, graphs, insights, leaderboard
├── 🎨 style.css     # All styling — theme, layout, components
└── ⚙️ script.js     # All logic — auth client, state, rendering, API calls
```

Yes, really — one file. No `npm install`, no bundler, no build pipeline.

---

## 🚀 Running Locally

Since it's a static single-file app, any static file server works:

```bash
python3 -m http.server 8000
```

Then open **http://localhost:8000** in your browser. 🎉

---

## 🔌 Backend Requirement

This frontend expects a compatible backend API URL, set via:

```js
window.TASKBOARD_API_URL = "https://your-backend-url.com";
```

Without a live backend, login and data syncing won't work — but you can still browse the UI locally.

---

## 🙌 Author

<div align="center">

Built with ❤️ by **[@shreyanshhh_08](https://t.me/shreyanshhh_08)**

📺 Channel: **[@aurabreaker7](https://t.me/aurabreaker7)**

⭐ If you like this project, consider giving it a star!

</div>

---

## 📄 License

No license specified yet — all rights reserved by the author.
