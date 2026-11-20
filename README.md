# Learning Cheat - The Smart Study Planner for BYU Students

A homework planner for BYU students. It reads your assignments from Canvas and
LearningSuite, works out which ones matter most right now, and gives you a short
list for today. It runs on your own computer and uses your own Claude, ChatGPT
or GitHub Copilot account to write the daily plan. Two of those can be free for
students.

## Install

You need a Mac. Linux should work, Windows hasn't been tried. Open Terminal and
paste this:

```bash
curl -fsSL https://raw.githubusercontent.com/jck-l-nrtn/learning-cheat/main/get.sh | bash
```

That line downloads the planner from this GitHub page into a `learning-cheat`
folder in your home folder, sets it up, and opens a setup page in your browser.
It never asks for your Mac password. If you'd like to read what it runs first,
open the same address in your browser. You don't need git, Xcode or Python.

The download is a few hundred MB: a private copy of Python if your Mac doesn't
have a recent one (from astral.sh), the Python packages the planner uses (from
pypi.org), and a separate copy of the Chromium browser that is only used to sign
in to LearningSuite. All of it stays in the folder.

The planner runs in that Terminal window. Closing the window stops it; nothing
is lost. Leave the folder where it is (moving it breaks the install and the
automatic runs). If you want it somewhere handy, make an alias to
`Learning Cheat.command` instead.

## Setup

The setup page asks for five things.

Canvas wants an access token. Go to Canvas, then Account, Settings, New Access
Token, and paste what it gives you. BYU tokens only last 30 days, so the planner
tells you a week before yours runs out and lets you paste a new one.

LearningSuite has no tokens, so a browser window opens and you sign in like
normal. Tick "remember this device" on the Duo screen and say yes when the
browser offers to save your password. Those two things are what let the planner
check your grades later without asking you again. It never sees your password.

Next is the AI that writes your daily plan and guesses how long things take.
Pick Claude, ChatGPT or GitHub Copilot. If it isn't on your computer the page
has a button that installs it, and another that opens its sign-in. You can skip
this. The planner still works, you just get a ranked list with no plan on top.
Which ones cost money and which are free is under
[Choosing an AI](#choosing-an-ai).

Then you pick which of the courses it found you want tracked, and click the days
and times each one meets. If a course shows up on both sites but only uses one, choose that one.

Last is your timezone, when you get up and go to bed, and the term dates.

If you're starting partway through the semester, leave "set aside work that was
due before today" ticked. Otherwise weeks of old assignments pile up at the top
of the list.

## Using it

Double-click `Learning Cheat.command` in the folder, or run `./plan app`. It
opens at http://127.0.0.1:8787.

Today is the plan. Upcoming is everything still open, in order. Needs attention
is where problems go: a login that expired, an assignment that vanished from the
gradebook, things you marked done by hand.

Calendar is your day, week or month and the free time in each day. Connect
Google Calendar, Outlook or iCloud with the calendar's private link, or add busy
times by hand: drag on an empty spot to add one, drag it to move or resize, click
it to edit or delete. Classes and work move the same way. All-day events don't
count as busy.

Settings, next to those tabs, is where you change things later: how many
credits a class is, when you get up and go to bed, the term dates,
your Canvas token and which AI writes the plan. It changes only the value you
edited in your files and keeps a copy of the file as it was. For a new term, or
to track different courses, run setup again from the link at the bottom of that
page.

The Sync, Grades and Replan buttons refresh things when you want. To have it
happen on its own, turn on Automatic runs in Settings (Mac only). That sets up a
6:30 am refresh and plan, a 2 pm grade check, and a Sunday evening pass that
estimates new assignments. macOS will show a "Background Items Added" notice
naming "bash"; that is these timers. During a run the Mac is kept awake, and if
the lid is closed the run waits up to four hours for you to open it. When grades
are checked, the LearningSuite sign-in window opens for a few seconds, signs in
with your saved password and closes. That is the planner.

It also keeps itself up to date. About once an hour while it is open, and during
the 6:30 am run, it checks this repository for a newer version and installs it,
including any new Python packages it needs. Your classes and data are left
alone. Updates are not separately signed: they are trusted because they come
over HTTPS from this GitHub account. To turn them off, put `updates:` /
`auto: false` in `config/prefs.local.yaml`.

## Bugs and ideas

This is a beta. At the bottom of every page are **Report a bug** and **Request
a feature**. Each opens a ready-made email to jck.l.nrtn@gmail.com with a few
questions and one line about your copy: its version, your macOS and Python
versions, which AI you chose, and whether Canvas and LearningSuite are set up.
It never includes your classes, assignments or grades, and you see the whole
email before you send it.

## How it works

A sync pulls every assignment from Canvas through its API, and from
LearningSuite by reading your gradebook, exam and schedule pages. Each one is
stored with its due date, how much of your grade it's worth, and whether it's
done. Something counts as done when it has a score, a submission, or you marked
it yourself.

Every open assignment then gets a score, roughly:

```
score = grade impact x urgency x distance x multipliers
```

Grade impact is the fraction of your final grade riding on it. This comes
straight from the gradebook when it's there. A 2% quiz and a 20% midterm are not
treated the same.

Urgency comes from slack, which is the free time you have before the deadline
minus the work left on it, minus the hours you already owe to things ranked
above it. Free time is whatever is left in your week after classes, work, meals,
sleep and whatever is on your calendar. With 40 or more hours of slack, urgency is nearly zero. At zero
slack it's at its maximum. Exams also ramp up on their own in the week before,
so a big one doesn't sit at the bottom of the list until the night before.

Distance pushes down anything more than ten days away, so a final in December
doesn't outrank this week's homework.

The multipliers are small nudges: exams count 1.6x, overdue work that can still
be turned in 2.5x, work that can't be turned in late 1.3x, something you've
already started 1.15x.

Once a day the top of that ranking goes to the AI you picked, along with how
much free time you have. It sorts the items into must do today, important, and if there's time,
and suggests minutes for each, capped at four and a half hours. The answer is
checked before you see it. Anything it made up is dropped, and anything due in
the next two days is always included.

For any item, `./plan why <id>` prints the numbers behind its rank.

Time estimates start as defaults per kind of work. The AI then reads each
assignment's description and guesses, and you can overwrite any of them. If you
log how long things really took, the estimates for that course adjust.

## Choosing an AI

The planner doesn't have an AI of its own. It runs the command-line tool from
one of these on your computer, signed in as you, so it uses the plan you already
have and there is no separate bill.

| | | Cost | What you need |
|---|---|---|---|
| <img src="src/web/static/img/claude.svg" width="28" alt=""> | **Claude** | Paid | A Claude Pro or Max plan |
| <img src="src/web/static/img/chatgpt.svg" width="28" alt=""> | **ChatGPT** | Paid, or free with a school account | ChatGPT Plus or higher, or a ChatGPT Edu account from your school |
| <img src="src/web/static/img/copilot.svg" width="28" alt=""> | **GitHub Copilot** | Free for students | A GitHub account with Copilot Student |

If you're paying for Claude or ChatGPT already, use that. If your school gives
you ChatGPT Edu, that's free and the simplest. Otherwise GitHub Copilot is free
for any student, it just takes a few days to get approved.

The setup page installs the tool and opens the sign-in for you. None of them
need Node, Homebrew or anything else installed first.

### Free with a school ChatGPT account

Choose ChatGPT in setup and sign in with your school account, not a personal
one. It only works if the school has left Codex turned on for students. If a
plan fails with a message about your workspace, that's why, and only the school
can change it.

### Free with GitHub Copilot

Copilot needs a student plan set up on GitHub's side before it will answer.

1. Make a GitHub account at https://github.com/signup if you don't have one.
2. Go to https://github.com/settings/education/benefits. If you aren't a
   verified student yet, click Start an application and finish it with your
   school email.
3. On the same page, under "Free GitHub developer resources for students and
   teachers", click Learn more and follow the prompts to turn on Copilot
   Student.
4. Back in the planner's setup page, choose GitHub Copilot, install it and sign
   in with that GitHub account.

GitHub can take a few days to approve a student. Until then your account has
Copilot Free, which also works here with a smaller monthly allowance. If GitHub
only shows you paid options, don't buy anything, just wait and try again.

Two things to know. GitHub doesn't say how big the free allowance is, and if it
runs out the plans stop until the first of the next month. And on Copilot Free,
GitHub may use what's sent to train its models unless you turn that off under
Privacy in your [Copilot settings](https://github.com/settings/copilot).

GitHub's own instructions are
[here](https://docs.github.com/en/copilot/how-tos/copilot-on-github/set-up-copilot/enable-copilot/set-up-for-students).

### Doing it by hand

These are the same steps the setup page runs, if you'd rather use Terminal or
need to fix something later.

Claude:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude auth login
```

ChatGPT:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex login
```

GitHub Copilot:

```bash
curl -fsSL https://gh.io/copilot-install | bash
copilot login
```

Each one opens a sign-in page in your browser. If the tool was already signed in
to a different account, the setup page has a "Use a different account" button
for Claude and ChatGPT. For Copilot, run `copilot`, type `/logout`, and sign in
again.

To switch later, open Settings in the planner and pick another one. `./plan setup --check` shows which is in use.

With Claude the plan is written by Opus and the estimates by Sonnet. If your
plan doesn't include Opus, set `plan_model: sonnet` under `agent` in
`config/prefs.local.yaml`. ChatGPT and Copilot use whatever model they pick by
default.

Gemini isn't offered. Google's command-line tool doesn't allow another program
to run it on a personal account.

## Your data

The planner's own files are kept in the folder. `.env` holds your Canvas token
(readable only by your Mac account) and `data/` holds your assignments, grades
and the sign-in browser with your saved LearningSuite password. Your token is
sent only to Canvas and your password only to BYU's sign-in page. Assignment
titles and descriptions, and how much free time you have, do get sent to Claude,
ChatGPT or GitHub Copilot, whichever you chose, when it writes the estimates and
the plan. That's the same as if you pasted them into a chat.

A linked calendar's private link is kept in `data/calendar.json`. Anyone with
that link can read the calendar. Event names are never sent to the AI.

Some things live outside the folder: the AI tool and its sign-in (in your home
folder; Copilot also keeps one in your Keychain), the three timers for automatic
runs (in `~/Library/LaunchAgents`), and the small "Sign in to ..." file the
setup page opens in Terminal (in your temporary folder).

The planner talks to Canvas, LearningSuite and BYU's sign-in (with Duo), the AI
company you chose, GitHub for updates, and the server of any calendar you link. While installing it also downloads
from astral.sh (Python, only if needed), pypi.org (packages) and Playwright's
download server (the browser).

To remove it, open Settings and follow "Removing the planner": turn off
automatic runs first, then delete the folder. Settings also lists what stays
behind (the AI tool, your Canvas token until it expires).

## Changing things

The numbers above live in `config/prefs.yaml`. Put your own values in
`config/prefs.local.yaml` so updates don't overwrite them. Your courses and
weekly schedule are in `config/courses.yaml` and `config/commitments.yaml`, and
`config/examples/` shows what else those files can hold.

`./plan --help` lists the commands. The ones worth knowing are `status` (is
everything syncing), `tasks` (the ranked list), `catch-up` (set aside overdue
work) and `update`.

There's more on the LearningSuite side in
[docs/LEARNINGSUITE.md](docs/LEARNINGSUITE.md). Tests run with
`./.venv/bin/python -m pytest tests -q` after installing `requirements-dev.txt`.

## License

MIT. See [LICENSE](LICENSE).

Claude, ChatGPT and GitHub Copilot are trademarks of Anthropic, OpenAI and
GitHub. Their logos are here only to show which account each option uses. This
project isn't affiliated with any of them, or with BYU.
