# The crowd study — setup, the waiver email, and the runbook

Three things, in the order they happen:

1. Ask Touro support for the **first-study fee waiver** (the email below). This
   is the one that takes time, so it goes first.
2. Set up the study on **CloudResearch Connect**.
3. Run it, using the runbook at the end.

Everything the developer has to do is written out here. The only things that
need a human at a keyboard are the sending of the email and the pasting of a
key.

---

## Part 1 — The fee-waiver email to Touro support

Send from your **Touro email address** to the Touro support/IT help address
your school publishes (the one that handles student requests for tools and
accounts). Attach nothing; the link to the study goes in when it exists.

> **Subject:** Request for first-study fee waiver — crowd research study
>
> Hello,
>
> I am a student at Touro and I am running my first human-subjects research
> study through CloudResearch Connect, the participant platform approved for
> academic studies. I would like to request the fee waiver that covers a first
> study, as described on CloudResearch's page for first-time researchers.
>
> The study is a short usability and quality review of a picture-book
> application I built. It takes about three minutes: a participant is shown one
> finished picture book and answers eight questions about it — whether the
> picture looks like the person in the photo, whether the story holds together,
> whether the book looks professional, and what is wrong with it. Ten
> participants in total. The book contains a cartoon drawing of the person whose
> photo was uploaded; no sensitive personal data is collected, no names are
> published, and participants are paid through Connect at the standard rate.
>
> The study will live at: *<paste the public study link once Connect gives it
> to you>*
>
> My Connect account email is: *<the email you registered with Connect>*
> My name and student id are: *<fill in>*
> My course and instructor are: *<Robinson's class, fill in the course code>*
>
> Could you let me know whether the waiver applies to my account, and whether
> there is anything else I should file before I launch?
>
> Thank you,
> *<your name>*

Two notes on sending it:

- **Send it before building the study.** Waivers are handled by people, and
  people take a day or two. If the waiver is not confirmed, the study still
  runs — it just costs about ten dollars instead of nothing.
- If support asks for the study link before they approve, send the email
  without it and reply with the link once Connect has generated it. Do not
  delay the request waiting for the link.

---

## Part 2 — Setting up the study on CloudResearch Connect

### Before you start

You need, in this order:

1. A **CloudResearch Connect** account, registered with your Touro email
   address (the same one the waiver request went from).
2. The **public link to the survey** in Engage. Build the survey first
   ([engage-survey.md](engage-survey.md)), then this.
3. The **live URL of the app**: `https://jack-and-the-beanstalk-book.onrender.com`.
4. Roughly **ten dollars**, unless the waiver is confirmed. Ten responses is
   the target; Connect charges per completed response.

### The setup, step by step

1. **Sign in** to CloudResearch Connect and choose to create a new study. Pick
   the "survey" or "external survey" study type — the study lives at an Engage
   link and Connect's job is only to recruit and pay the participants.
2. **Paste the Engage link** as the study URL. When Connect asks for a
   completion code, say that no code is needed: the survey is short, and
   requiring a code adds a step that costs you a drop-off. (If Connect insists,
   turn on the code and put the final screen of the Engage survey in front of
   it.)
3. **Recruitment filter.** Two settings matter:
   - **Location:** United States. The book is in English and the drawing
     defaults read as a western storybook style.
   - **Age:** 18 or over.
4. **Google account requirement.** The app asks a participant to sign in with
   Google so their book waits for them. Ask Connect to screen for participants
   **with a Google account** (most have one; the ones who do not cannot get in
   and would be a wasted response). Put the same line at the top of the study
   intro so nobody is surprised: *you will need a Google account to sign in*.
5. **Time estimate.** Set 5 minutes (three minutes of work, with slack for a
   slow first page load). Under-promising the time is how you get a study that
   people abandon halfway.
6. **Pay per response.** Use Connect's default for a five-minute study. Ten
   responses at the standard rate is the roughly-ten-dollar figure the plan
   quotes; with the first-study waiver it is nothing.
7. **Approval.** Connect reviews the study before it goes live. Expect that to
   take up to a day, which is another reason step 1 (the waiver email) goes
   first.
8. **Pilot with one response.** Buy *one* response before buying all ten. Open
   the Engage dashboard and read it. If question 5 and 6 are coming back empty,
   or if people are answering Q1 with "I cannot tell", fix the survey before
   spending the rest of the money — that is what the pilot is for.

### What the deployment should be running during the study

One decision worth making before the study opens, because it changes what the
responses mean:

**Run the study on the deployment that produces real pictures, not free
placeholders.** The whole point of Q1 ("does it look like them") is to hear a
real person answer it about a real drawing. A run on a free provider that draws
a different face turns Q1 into noise, and the cheapest way to lose a study is to
collect ten answers to a question you broke on purpose.

- **Recommended:** `IMAGE_PROVIDER=openai` at `quality=low` — the same
  configuration the graded hand-in uses. Ten books is twenty calls each, about
  **$1.20 in total**. That is the whole cost of the study's pictures.
- **Rehearse first, for free:** before opening the study, run the whole flow
  yourself on `IMAGE_PROVIDER=agnes` (the free testing provider) — upload,
  sign in, approve a portrait, let the book render, download the PDF, paste the
  PDF link into Engage, walk the survey once as a participant. That costs
  nothing and catches every broken link, which is the failure that actually
  loses a study.
- Do not run the study on `IMAGE_PROVIDER=mock`. The pictures come back with
  "DEMO PICTURE — NOT A REAL DRAWING" written across them, which answers Q3 for
  the participant before they have looked.

---

## Part 3 — The runbook for the study window

Do these in order on the day.

**Before it opens**

- [ ] Waiver answer received, or the ~$10 accepted as spend.
- [ ] Engage survey built, timed, and piloted once by the developer.
- [ ] Deployment on the real provider; `/healthz` reports healthy.
- [ ] One full run by the developer on the live URL: Google sign-in → photo →
      portrait → approve → book renders → PDF downloads.
- [ ] The PDF opens in the Engage embedded viewer on a phone as well as a
      desktop. If the viewer will not embed the PDF, host it as a public link
      and give participants a "open the book" button instead — the study needs
      the whole document visible, not a download.
- [ ] Connect study submitted for approval.
- [ ] One pilot response bought, read, and the survey fixed if it needed fixing.

**While it runs**

- Watch the deployment's logs for books that fail. A participant whose book
  fails to render answers the questions about a page that never loaded, which
  is a wasted response — and it is visible in the log as `book.failed`.
- Do not change the app while the study is open. A redeploy mid-study is a
  silent restart: the picture-book job is process-bound, and a book that was
  half-rendered when the process restarted is lost.

**When the ten are in**

- [ ] Export the ten responses to a spreadsheet, one row per participant.
- [ ] Download the ten PDFs that were actually shown, and file them next to the
      responses — the answers only mean something against the exact book they
      were given.
- [ ] Fill in [comparison-template.md](comparison-template.md).
- [ ] Publish the comparison, and only then look at the AI-persona method
      ([ai-persona-plan.md](ai-persona-plan.md)) for the class comparison.
