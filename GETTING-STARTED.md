# Getting started with BirdBrain — the gentle version

This turns a small computer into a little robot that **listens to a live wildlife
camera and tells you which birds it hears.** This guide walks you through setting it
up, one small step at a time. You don't need to be a computer expert — just follow
along and copy each line exactly.

☕ **Set aside about an hour, and make a cup of tea** — one step does a lot of
downloading and you'll mostly be waiting.

---

## What you'll need

- **A computer** — a Raspberry Pi, or an ordinary Mac or Linux computer.
- **An internet connection.**
- That's it. You won't need to buy anything.

## A word about the "Terminal"

You'll be typing into a program called the **Terminal** — a plain window where you
type instructions instead of clicking buttons. It looks old-fashioned, but it's just
a place to give the computer typed commands.

For each step below: **copy the line, paste it into the Terminal, and press Enter.**
Then wait for it to finish before doing the next one.

> 💡 To paste into a Terminal: on a Mac use **⌘ + V**; on **Windows PowerShell**
> just **right-click** (or Ctrl + V); on a Pi/Linux terminal it's usually
> **Ctrl + Shift + V**.

**To open the Terminal:**
- **Raspberry Pi / Linux:** click the menu and look for "Terminal" (or press
  `Ctrl + Alt + T`).
- **Mac:** press `⌘ + Space`, type `Terminal`, press Enter.
- **Windows:** click **Start**, type `PowerShell`, and open **Windows PowerShell**.

---

## Step 1 — Install the helper programs

BirdBrain needs a couple of free helper programs to listen to the sound. Paste the
block for your computer.

**On a Raspberry Pi or Linux:**

```bash
sudo apt update
sudo apt install -y ffmpeg git nodejs
```

> When it asks for your password, type it and press Enter. **You won't see the
> letters appear as you type — that's normal and on purpose.** Just keep typing and
> press Enter.

**On a Mac** (this also installs "Homebrew", the thing that installs programs):

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install ffmpeg git node
```

**On Windows** (in PowerShell):

```powershell
winget install Git.Git
winget install Gyan.FFmpeg
winget install OpenJS.NodeJS.LTS
```

> After it finishes, **close PowerShell and open a fresh one** so it can see the new
> programs. (If it says `winget` isn't found, update "App Installer" from the
> Microsoft Store, then try again.)

## Step 2 — Install "uv" (the program that sets everything up)

**On a Raspberry Pi, Linux, or Mac:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**On Windows** (in PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

When it finishes, **close the Terminal window and open a fresh one** so it knows the
new program is there.

> ✅ From here on, **every command is the same** whether you're on a Pi, a Mac, or
> Windows — just type them into your Terminal (PowerShell on Windows).

## Step 3 — Download BirdBrain

```bash
git clone https://github.com/CaiusX/birdbrain.git
cd birdbrain
```

(The first line downloads BirdBrain. The second line steps *into* its folder — like
double-clicking to open it.)

## Step 4 — Let it get ready (this is the slow one) ☕

```bash
uv sync
```

This downloads everything BirdBrain needs to recognise birds. **It can take quite a
while — several minutes, longer on a Raspberry Pi.** It's normal for it to look busy
or pause. This is your tea moment. Wait until you get the normal text prompt back.

## Step 5 — Tell it what to listen to

```bash
cp sources.example.toml sources.toml
```

That's it — you don't have to edit anything to begin. This copies a ready-made
settings file that's **already pointed at a live African wildlife camera**, so
BirdBrain has something to listen to straight away. (Later, a tech-savvy friend can
swap in different cameras or a microphone.)

## Step 6 — Start it up

BirdBrain has two parts that run at the same time, so you'll use **two Terminal
windows**.

**Window 1 — the listener.** In your current window, type:

```bash
uv run africam run
```

Leave this window open and running. It's now listening for birds. (It may be quiet
for a bit — that's fine.)

**Window 2 — the web page.** Open a **second** Terminal window, then type:

```bash
cd birdbrain
uv run africam web --host 0.0.0.0 --port 8765
```

Leave this one open and running too.

## Step 7 — Look at it! 🐦

Open your web browser (Chrome, Safari, Firefox…) and go to this address:

```
http://localhost:8765
```

You should see the BirdBrain dashboard — a map, and a list of birds as it hears them.
**Congratulations, it's running!**

> Want to see it from your phone or another computer on the same wifi? On the
> computer that's running BirdBrain, find its address (on a Pi/Linux type
> `hostname -I`), then on the other device visit `http://THAT-ADDRESS:8765`.

---

## Stopping and starting again

- **To stop** either part: click that Terminal window and press **Ctrl + C**.
- **To start again later:** open a Terminal, type `cd birdbrain`, then run the two
  commands from Step 6 again (one in each window).

## If something doesn't look right

- **It seems frozen during Step 4.** It's almost certainly just working — that step
  is slow. Give it more time before worrying.
- **It says `ffmpeg: not found` or `git: not found`.** Step 1 didn't finish — run it
  again.
- **The web page won't open.** Make sure **Window 2** (Step 6) is still open and
  running, and that you typed the address exactly: `http://localhost:8765`.
- **No birds appear for a while.** That's normal — it only logs a bird when the
  camera actually picks one up. Leave it running.
- **Still stuck?** Show a tech-savvy friend the more detailed guide,
  [`INSTALL.md`](INSTALL.md).

## Want it to run on its own, all the time?

Leaving the two windows open works fine for trying it out. To make it start by itself
and run 24/7 (and to share it safely on the internet), that part *is* more technical —
point a helper at [`INSTALL.md`](INSTALL.md), which has the step-by-step.

---

*BirdBrain is free to use for learning and fun. The bird-recognition brains
(BirdNET) are free for non-commercial use. Enjoy your birds!* 🌿
