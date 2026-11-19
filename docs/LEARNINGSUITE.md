# BYU LearningSuite integration

How the planner reads LearningSuite, and what to do when it breaks. The facts
here were measured against the live site in November 2026. Code: `src/ls/` (session, browser,
parse, refresh) and `src/sources/ls_gradebook.py`, `ls_prep.py`, `ics_feed.py`.

## 1. Pipeline

```
get_page (requests) -> classify -> parse (Python, on raw HTML) -> build_dump -> atomic write
                                                   data/learningsuite/ls-<courseID>.json
```

Per course, three pages: gradebook (required), exam and calendar (optional; if
one fails the course still refreshes, with a warning, and the previous values
are carried over). `./plan sync` then reads the dumps via
`src/sources/ls_gradebook.py`; it never touches the network.

## 2. Authentication

LearningSuite is an OIDC client of BYU's Okta (`login.byu.edu`); CAS is a hop
inside Okta. Duo sits in the Okta flow.

- Cookies that persist in the dedicated browser profile: `login.byu.edu` `DT`
  and Duo `browsertrust` (the device-trust cookie), among others.
- Cookies that do not persist, because they are session cookies: LearningSuite
  `PHPSESSID`, Okta `idx`, CAS `TGC`. That is why a browser restart signs you out.
- `PHPSESSID` alone is enough to read every student page. While the Okta session
  is alive, a dead LearningSuite session renews itself through ordinary redirects
  (`learningsuite -> login.byu.edu -> /oidc/callback.php -> page`), and `requests`
  follows them. Both sessions die somewhere between 20 minutes and 3 hours
  idle, so no saved cookie survives a night.

Scheduled runs mostly fire with the lid closed, during "dark wakes" of 2 to 30
seconds; a sign-in cut off by the Mac going back to sleep was the main cause of
failed mornings (measured Nov 9-11 2026 against `pmset -g log`). So the whole
attempt runs under `caffeinate -i` (`src/ls/power.py`), an `offline` result
waits up to 90 s for Wi-Fi and retries, and a failure while the lid is still
closed waits `learningsuite.wait_for_wake_minutes` (default 240) for the lid to
open and retries once. One refresh runs at a time (`.refresh.lock`).

The chain `src/ls/refresh.py` runs on each attempt:

1. If `learningsuite.save_session` is true and the run is not interactive, load
   `data/.ls-session.json` and request the first course's gradebook.
2. `offline` is reported if the network fails. `logged_out` (or no saved session)
   goes on to a sign-in, unless `--no-browser` (then `logged_out`).
3. `session.online()` pings the public iCal endpoint to separate "no network"
   from "not signed in".
4. `browser.login()` opens the Playwright profile `data/.browser-profile`
   (headed: Duo device trust is bound to the visible browser), clicks Sign In
   (the browser fills the saved password; the code never reads or types it),
   waits up to 60 s of running time (300 s for `ls-login`), and returns the
   cookies and user agent. Unattended, it submits the form only when the
   password is filled in, at most three times, and stops if the page answers
   with an error. `logs/ls-login.log` keeps the trail across runs.
5. Everything else is plain HTTP with those cookies. If `save_session` is on and
   any gradebook was read, the cookies for `learningsuite.byu.edu`, `login.byu.edu`
   and `cas.byu.edu` are saved to `data/.ls-session.json` (mode 600). With it off,
   cookies are never written, so every run signs in again (about ten seconds).

`--via-browser` loads each page in the browser and runs the same parser on the
HTML; it does not save a session. `ls-login` ignores any saved session.

Treat `data/.browser-profile` and `data/.ls-session.json` as logins to your account.

## 3. URLs and pages

```
https://learningsuite.byu.edu/.x/cid-<COURSE_ID>/student/<page>
```

`<COURSE_ID>` is stable per course (`config/courses.yaml` `ls_course_id`, or the
`courseID=` of the course's iCal URL). The `.x` segment is a placeholder: the
server answers with a tiny page (under 8000 bytes) whose script sets
`location.href = "/.<token>/cid-..."`. It is not an HTTP redirect, so
`session.get_page` reads that path and fetches it (up to three hops).
`parse.classify` decides what any response is, before parsing, in this order:

| Condition | Kind |
|---|---|
| URL on a login host (`login.byu.edu`, `cas.byu.edu`, `id.byu.edu`, `/oauth2/`, duosecurity), or a bare 30x | `logged_out` |
| HTTP 400+ | `http_error` |
| host is not `learningsuite.byu.edu` | `page_changed` |
| page marker present | `ok` |
| small page with a `location.href` | `loader` (follow it) |
| Okta sign-in form on a LearningSuite URL | `logged_out` |
| anything else | `page_changed`, with the page size and the missing marker |

| Page | Marker | Embeds |
|---|---|---|
| `gradebook` | `studentAssignmentsData` | `const studentAssignmentsData = { categories: [...], assignments: [...], scores: [...], ... }`: a JS object with bare keys whose values are each valid JSON (23 keys) |
| `exam` | `app/drivers/student/exam/` | `const args = {"exams": [...], ...}` |
| `calendar` | `eventArray` | `const eventArray = [...]` with `columns`, `assignments`, `classDays`, `teachingDays` beside it |

`parse.js_const` finds the declaration and cuts out the literal with a
bracket-matching scanner (only double quotes delimit strings). Used keys:

- `studentAssignmentsData`: `categories` (`id, title, weight, graded, extraCredit,
  equalAssignWeight, lowScoresToDrop`), `assignments` (`id, gbAssignmentID,
  categoryID, typeID, name, dueDate, beginDate, points, weight, extraCredit,
  graded, includeScoreInFinalGrade, allowLateSubmission`, description, ...),
  `scores` (`assignmentID, score, excuse, modifyTime, deleted`), `submissions`
  (`assignmentID, modifyTime`), `completed` (`assignmentID, completed`),
  `useCategoryWeights`, `gradeScale`. `student` holds a name and IDs and is dropped.
- `args.exams[]`: `id, title, status` (`complete`, `incomplete`, `active`),
  `timesTaken`, `isScoringComplete`.
- `eventArray[]`: `serverDate, name` and `column`.

`dueDate` is `YYYY-MM-DD HH:MM:SS` local time with no zone; the code applies the
`commitments.yaml` timezone. Other student pages (home, syllabus, pages,
discuss) exist but are not read.

## 4. Id namespaces and join keys

An assignment has two ids and a type id. Mixing them silently matches nothing,
which is why the parser raises `PageChanged` when scores exist but none join.

| Join | Key |
|---|---|
| score to assignment | `score.assignmentID == assignment.gbAssignmentID` (never `assignment.id`) |
| submission to assignment | `submission.assignmentID == assignment.gbAssignmentID` |
| exam to assignment | `exam.id == assignment.typeID` |
| "mark complete" tick | `completed[].assignmentID == assignment.id` (your own tick; only a note, never closes a task) |
| assignment to category | `assignment.categoryID == category.id` |

The planner's task uid is `ls:<assignment.id>`. Findings: 156 of 156 scored rows
join on `gbAssignmentID` and 0 on `id`; 97 of 97 exams join on `typeID`.

"Done" per assignment (`ls_gradebook._status`, first match wins): a score (even
0) is `graded`; `excuse` is `dropped`; exam status `complete` is `submitted`
(`exam_complete`); a submission is `submitted` (`submission`). In-class and paper
work leaves no trace until its score is entered, so only you can close it.
Categories named attendance, participation, iClicker or in-class are unschedulable
(zero effort) unless the name also says evaluation, essay, quiz and the like.

## 5. Grade weights

`useCategoryWeights` is a per-course switch. `impacts()` in
`src/sources/ls_gradebook.py` computes the fraction of the final grade per
assignment. An assignment counts only if its category is `graded` and not
`extraCredit`, it is not `extraCredit`, and `graded` and `includeScoreInFinalGrade`
are not false. Everything else gets 0.

**`useCategoryWeights` false**: straight points.
`impact = points / sum(points of counting assignments)`. Category weights are ignored.

**`useCategoryWeights` true**:
`impact = cat_share * share`, where
`cat_share = category.weight / sum(weight of graded, non-extra-credit categories)`
(weights are relative integers and need not sum to 100), and within the category
among counting members:

- `equalAssignWeight`: `share = 1 / count`
- else if the members' own `weight` values sum above 0: `weight / sum(weight)`
- else `share = points / sum(points)`

A dump without the flag is treated as category-weighted. A course's impacts can
sum to less than 1 when part of its grade is hand-entered in
`manual_assignments.yaml`.

`tests/test_ls_parse.py::test_impacts_match_displayed_weights` compares every
percentage the saved gradebook pages display (231 of 231 across the four
courses, within 0.011 points). A category that drops its lowest N scores gives
`category_drops`, which ranks the task at 0.85x; it is a discount, not permission
to skip.

## 6. Refresh states

`./plan ls-refresh` ends each course in one state, recorded with a timestamp in
`data/learningsuite/status.json` (`last_ok` keeps the last success). The first
course's gradebook decides offline and logged-out for all courses.

| State | Meaning | What to do |
|---|---|---|
| `ok` | dump written | nothing; warnings name an optional page that was not read |
| `offline` | network unreachable, after waiting 90 s for it | retry later |
| `asleep` | the lid stayed closed for the whole wait, so sign-in never got to run | nothing; the next run retries |
| `logged_out` | sign-in needed or failed; the detail says which (Duo wants approval, no saved password, the page refused the login, or `--no-browser`) | `./plan ls-login` |
| `page_changed` | page loaded but is not the expected shape | edit `src/ls/parse.py`; section 9 |
| `http_error` | HTTP 400+ from LearningSuite | retry later |
| `empty_course` | valid page, zero assignments (counts as ok) | nothing |
| `rejected` | parsed, but the previous dump had 10 or more assignments and this has under half | old dump kept; look at the course page |

A dump is replaced only by a valid one. If the exam page fails, each
assignment's previous `examStatus` is kept; if the calendar fails, the previous
`schedule`. `./plan status` shows them (stale after 36 h).

## 7. Dump format (schema 2)

`data/learningsuite/ls-<courseID>.json`, no student identifiers (`studentID`,
`ownerID` are removed):

```
schema, courseID, extractedAt, useCategoryWeights, gradeScale,
categories[], assignments[],
schedule {events [{date, column, text}], columns []} | null,
counts {assignments, scoreRows, scoresJoined, submissions, exams, examsJoined, scheduleEvents},
pages {gradebook, exam, calendar}      # which pages were read this time
```

Each assignment keeps its source fields and adds `score`, `excused`, `scoredAt`,
`submittedAt`, `selfCompleted`, `examStatus` and `examTimesTaken`. Only the newest
dump per `courseID` is used. The schedule is stored, but no source reads it yet.

## 8. The iCal feed and the RPC

**iCal feed**: `https://learningsuite.byu.edu/iCalFeed/ical.php?courseID=<ID>`,
public, regenerated about daily. The ID is the credential, so keep feed URLs in
`.env` (`ls_ics_url: "${NAME}"`). It is a teaching outline: no points, weights or
scores, and coverage depends on the instructor. Used by `ics_feed.py` (only for
courses with no gradebook dump, filtered by an allowlist, overridable with
`ls_filter`) and `ls_prep.py` (`prep_tasks`: ungraded class preparation). Quirks
handled in `ics_feed.py`:

- Not valid UTF-8: upstream turns byte `0xA0` into `0x20`, breaking characters
  like 根 (`e6 a0 b9` becomes `e6 20 b9`). `repair_mangled_utf8` restores a space
  where a continuation byte belongs.
- Fields are concatenated without separators (`"No classSecond Exam"`);
  `clean_title` re-inserts spaces first.
- Duplicates: same-day entries collapse by their ASCII title ignoring
  parentheticals; a multi-day testing window collapses to its first day and keeps
  parentheticals (so "Unit 1 Exam (Listening)" and "(Reading)" stay apart).
- All-day `DTEND` is exclusive; one day is subtracted. All-day items are due 23:59.
- Fetches fall back to the cached copy in `data/cache/`.

**RPC**: `POST /ajax.php?appId=<appID>&subsessionID=<token>[&view=<viewID>]`,
form-encoded. Only the Content tab uses it (`getPublishedCoursePages`,
`readPage`); the planner does not call it. Reconstructed from `components-build.js`:

| Field | Notes |
|---|---|
| `funcName` | server function |
| `funcParams` | JSON-encoded params |
| `currentPage` | `true` for page-scoped calls |
| `pageId` | `global_pageID`, sent when `appId === "student"` |
| `url`, `classname`, `contructorParams`, `isPage` | non-page-scoped calls |

`contructorParams` is misspelled in their source; match it. Uploads go to
`ajaxFiles.php` as multipart.

## 9. When LearningSuite changes its page

The signal is `page_changed` in `./plan ls-refresh` or `./plan status`; the
detail names the missing key or marker. The saved-page tests will not fail on
their own, since the fixtures are frozen. They are what you extend.

1. Capture the new page with a session, saving the HTML and nothing else:
   `from src.ls import session as s; st, html = s.get_page(s.load(), "<courseID>", "gradebook")`
   (needs `save_session`, or take cookies from `browser.login()`). Do not print cookies.
2. Replace or add `tests/fixtures/ls/<COURSE>-{gradebook,exam,calendar}.html`.
   Raw pages contain a `student` object (name and ids); scrub it. Rebuild
   `<COURSE>-displayed-weights.json` (assignment id to the percent the page shows).
3. Edit `src/ls/parse.py`: `MARKERS`, `parse_gradebook` (required keys, row keys,
   the `gbAssignmentID` join), `parse_exams`, `parse_schedule`. Bump `SCHEMA` only
   if the dump shape changes, and update `ls_gradebook` and `refresh.build`.
4. Run `./.venv/bin/python -m pytest tests -q`. Expect to touch, in
   `tests/test_ls_parse.py`: `test_gradebook_counts` and the `ASSIGN_N`, `SCORES_N`,
   `EXAM_N`, `USE_CAT` tables, `test_scores_joined_via_gb_assignment_id`,
   `test_impacts_match_displayed_weights` and `test_classify_real_fixtures_ok`. The
   `test_shape_*` tests show which rename produces which `PageChanged` message.
5. For a weighting change, fix `impacts()` until the displayed-weights test passes
   again. Fixtures are real data; do not loosen the 0.011 tolerance.
