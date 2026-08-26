# CaseWeave — User Guide

> Weave requirements into test cases. Upload a requirements doc → the system clarifies ambiguities first → it generates structured test cases and exports them to Excel.

[简体中文](USAGE.md) | **English**

**Live demo: <http://47.93.126.69:3001/>**

To run your own instance, see the [deployment guide](DEPLOYMENT.md) (in Chinese); for the code layout, see [CLAUDE.md](../CLAUDE.md).

> **Note on language:** the product UI is in Chinese. Throughout this guide, every button and page is given in English with the on-screen Chinese label in brackets, e.g. **Generate Test Cases**「生成测试用例」, so you can follow along in the real interface.

---

## Contents

- [1. Five-minute quick start](#1-five-minute-quick-start)
- [2. Accounts, sign-in and projects](#2-accounts-sign-in-and-projects)
- [3. Interface tour](#3-interface-tour)
- [4. Main flow: generating test cases](#4-main-flow-generating-test-cases)
- [5. Alternate flow: generating a test mindmap](#5-alternate-flow-generating-a-test-mindmap)
- [6. Case management and Excel export](#6-case-management-and-excel-export)
- [7. Knowledge base (work in progress)](#7-knowledge-base-work-in-progress)
- [8. Feedback and self-evolution (work in progress)](#8-feedback-and-self-evolution-work-in-progress)
- [9. Prompt management](#9-prompt-management)
- [10. Limits and quotas](#10-limits-and-quotas)
- [11. FAQ](#11-faq)
- [12. Getting more out of it](#12-getting-more-out-of-it)

---

## 1. Five-minute quick start

1. Open <http://47.93.126.69:3001/>, register with an email address and sign in.
2. Pick a project from the list (if there is none available, ask an admin to create one or make one public).
3. In the left column, click **Generate Test Cases**「生成测试用例」to start a new session.
4. On the guidance card, **upload a requirements document** (`.docx` / `.pdf`) — or just paste a **Lark (Feishu) doc link**.
5. Once parsing finishes, click **Start**「开始生成」and walk through:
   **confirm module → review knowledge drafts → answer clarification questions → generate cases**.
6. When the cases appear, rate / edit them inline, then go to **Case Management**「用例管理」and click **Export Excel**「导出 Excel」.

Step 5's **clarification Q&A** is the only part that really needs your attention — the more specific your answers, the better the cases.

---

## 2. Accounts, sign-in and projects

### 2.1 Accounts

- The landing page is the sign-in / sign-up form: **email + password (≥ 6 characters)**, display name optional.
- The session token is stored in the browser and lasts 7 days by default; just sign in again when it expires.
- **Admins** are determined by the `ADMIN_EMAILS` setting at deploy time. Regular accounts can join existing projects right after registering.

### 2.2 Projects

After signing in you land on the **project picker**. A project is the unit of data isolation — sessions, test cases, the knowledge base, Skills and prompt versions are **all scoped per project**.

| Role | Can do |
|------|--------|
| Admin | Create / delete projects, toggle a project between **public and private**, view token usage across all accounts |
| Regular user | Enter public projects (or ones they have access to) and use every feature |

> Deleting a project **also wipes every session and test case inside it**, irreversibly. Handle with care.

Sharing one project across a team is encouraged: everyone's feedback and accumulated knowledge feed the same evolution loop.

---

## 3. Interface tour

Inside a project the workspace has three columns.

**Far-left rail — four pages**

| Icon | Page | Purpose |
|------|------|---------|
| 💬 | **Chat**「对话」 | Upload docs, clarify, generate cases / mindmaps |
| ✅ | **Case Management**「用例管理」 | All cases across sessions: filter, edit, export to Excel |
| 📖 | **Knowledge Base**「知识库」 | Accumulated product rules / constraints / glossary, plus modules, Skills and documents |
| 📈 | **Evolution Report**「进化报告」 | Negative-feedback digests — what the system has learned |

**Middle — session list**

- Two buttons at the top: **Generate Test Cases**「生成测试用例」and **Generate Test Mindmap**「生成测试脑图」— one click creates a session.
- Sessions can be **renamed by double-clicking** and deleted (deleting also removes that session's cases and clarification history).
- Sessions are independent; keep several open and switch freely.

**Right — main chat area**

A **step bar** stays pinned at the top so you always know where you are:

- Case mode: `upload material → confirm module → review knowledge → clarify → generate cases`
- Mindmap mode: `upload requirements doc → clarify → generate mindmap → save to Lark`

The composer sits at the bottom; the icons to its left are: upload PRD (`.docx`/`.pdf`), upload test mindmap (`.md`), paste a mindmap outline, and import from a Lark link.

---

## 4. Main flow: generating test cases

### Step 1 — Upload material

A new "Generate Test Cases" session opens with a **Step one: upload material**「第一步：上传资料」card offering four entry points:

| Method | Formats | Notes |
|--------|---------|-------|
| Upload requirements doc | `.docx` / `.pdf` | The common case. Body text and tables are both parsed |
| Upload test mindmap | `.md` (Markdown outline) | Seeds generation with test thinking you already have |
| Paste mindmap outline | Markdown text | Copy straight out of your mindmap tool |
| Lark link | Lark doc / wiki link | Several links at once; requires `lark-cli` configured on the server |

**You can upload the PRD, the mindmap, or both.** With both, the system merges them and **the mindmap wins on conflicts**.

Progress is streamed live (validate → fetch → parse → clarify). When parsing completes, a file chip shows the character count and whether the text was **truncated** (default cap: 30,000 characters per document; anything beyond is cut and clearly flagged).

Once the material is in place, click **Start**「开始生成」.

### Step 2 — Confirm the module

The system decides which **module** the document belongs to (e.g. "Order Management") and shows a **module confirmation card** for you to sign off:

- Correct match → confirm as is;
- Wrong guess → pick another from the dropdown;
- Genuinely new area → **create a module**「新建模块」with:
  - **Module name** (e.g. `订单管理`)
  - **English name / case-ID prefix**: must **start with an uppercase letter**, contain only `A–Z`, `0–9` and hyphens, 1–40 characters, e.g. `ORDER-MGMT`. This becomes the prefix of generated case IDs.
  - One-line description (optional)

Modules organise the knowledge base, Skills and case filtering — **get this right and everything downstream lands in the right drawer**.

### Step 3 — Review knowledge drafts

The system extracts product knowledge worth keeping (rules, constraints, glossary terms, UI behaviour) as **drafts** for you to review:

- Tick the entries to write **permanently into the project knowledge base**;
- The pencil icon lets you revise the text, change the type, adjust the confidence;
- Exact duplicates are filtered out automatically;
- Entries **similar to** or **conflicting with** existing ones are listed side by side with the reasoning, and you decide per entry:
  **keep the new one (replacing the old) / keep the old one / keep both** (default: keep both);
- Or handle the batch at once with **store all**「全部入库」/ **discard all**「全部丢弃」.

Approved knowledge is retrieved and injected into every later generation — a main reason the system gets sharper the more you use it.

> If you want to move fast, store everything now; you can edit or delete entries any time on the Knowledge Base page.

### Step 4 — Clarification

This is the step that matters most. The Clarifier Agent reads the document alongside the knowledge base and turns **ambiguities, gaps and fuzzy boundaries** into questions — e.g. "the timeout isn't specified; after how many seconds does this count as a timeout?"

- **Answer question by question**; your answers go straight into the generation context;
- After each round the model decides whether to keep probing — **up to 5 rounds**;
- If nothing is ambiguous, it goes straight to generation;
- For questions you genuinely can't answer, write "use the default / TBD" — but **vague answers buy vague test cases**.

On the final round the button changes to **Submit answers and generate test cases**「提交回答并生成测试用例」.

### Step 5 — Generate

Generation is streamed and can be **stopped** mid-run. The output is a structured table:

`Case ID / Case name / Module / Priority / Preconditions / Steps / Expected result / Notes`

Right in the table you can:

- ✏️ **Edit any field inline** (edits are recorded as feedback and feed the evolution loop)
- 👍 **Like** / 👎 **Dislike** (a dislike can carry a reason, e.g. "missed the boundary case", "steps too coarse")
- 🗑️ Delete a single case (with confirmation)

---

## 5. Alternate flow: generating a test mindmap

If what you want is a **map of test thinking** rather than an Excel sheet, choose **Generate Test Mindmap**「生成测试脑图」when creating the session.

The flow is shorter: `upload requirements doc → clarify → generate mindmap → save to Lark`

- Only a PRD is needed (local file or Lark link);
- Clarification works the same way (up to 5 rounds); the final button reads **Submit answers and generate test mindmap**「提交回答并生成测试脑图」;
- The result is **written straight into Lark**, with a clickable link on the card and a **regenerate** option.

A common pattern: **run the mindmap first to align on test thinking → adjust it by hand → then open a case session and feed that mindmap together with the PRD.**

---

## 6. Case management and Excel export

**Case Management**「用例管理」is the **cross-session aggregate view**, grouping every case in the project by module.

- **Filter** by module, priority, creation time;
- **Inline edit**: double-click a cell, changes save immediately;
- **Delete** one case at a time, with confirmation;
- **Column widths**: drag the header edge; the browser remembers them;
- **Like / dislike**: same as in the chat table, and it drives evolution just the same.

**Export Excel**「导出 Excel」in the top right downloads **the current filtered set**. The workbook:

- Uses **one sheet per module**; cases without a module go to "其他" (Other);
- Freezes the header row and pre-sizes the columns;
- Mirrors the on-screen fields and adds a **Self-test result**「自测结果」column to fill in during execution.

---

## 7. Knowledge base (work in progress)

The **Knowledge Base**「知识库」page holds everything the project has accumulated:

- **Knowledge entries** — product rules, constraints, glossary terms, UI behaviour, each with a type, a confidence score and an owning module. Searchable, filterable by module, editable, deletable; project-level (unassigned) entries can be **filed into a module**.
- **Modules** — create / edit / delete, plus **relations between modules** (e.g. "Orders" depends on "Payments"), which widen the knowledge retrieval scope.
- **Skills** — reusable test-design rules per module, distilled by the system from your edits and negative feedback (you can also write them by hand or regenerate them), injected at generation time.
- **Documents / mindmaps** — the raw material already gathered under each module.

All of this is **semantically retrieved** during clarification and generation, with the most relevant entries injected into the prompt. So: **the cleaner the knowledge base, the closer the output is to your product.** Deleting a wrong entry is the single most effective cleanup you can do.

---

## 8. Feedback and self-evolution (work in progress)

The system runs on three kinds of fuel, all of them ordinary things you do anyway:

| Your action | How it's used |
|-------------|---------------|
| 👍 Like | A positive sample that reinforces the current style |
| 👎 Dislike (+ reason) | Normalised into an intent and routed to a concrete outlet |
| ✏️ Edit a case | The before/after diff is analysed to infer what generation got wrong |

Negative feedback is normalised into an intent (e.g. "add boundary cases", "fix a business rule", "steps too coarse") and routed to **three outlets**:

- **System prompt** — becomes a prompt-improvement suggestion (a draft; a human must adopt it)
- **Skill** — distilled into a test-design rule for that module
- **Knowledge base** — stored as a product rule or constraint

The **Evolution Report**「进化报告」page is the ledger for this loop: the dislike reason, which fields were changed, the related case, the intent distribution, **which outlet it flowed to**, and **whether that outlet has consumed it yet** ("pending" / "digested").

> In one line: **write a reason when you hit dislike** — it's the highest-value input you can give this system.

---

## 9. Prompt management

The chat page opens a **System Prompt Manager**「系统提示词管理」drawer covering three system prompts (**isolated per project**):

- `clarifier_initial` — first clarification round
- `clarifier_followup` — follow-up rounds
- `generator` — test case generation

From there you can:

- View the **default version** (the built-in "original suggested version") and the full **version history**;
- Edit and save as a **new version**, **activate** one with a click, **roll back** or **reset to default** at any time;
- Review **improvement suggestions** the system drafts from negative feedback (for `generator`) and **adopt** them one by one (adopting creates a new version) or dismiss them.

> Recommended: **system-generated suggestions are always drafts and never activate themselves** — read the stated rationale before adopting. If a version turns out worse, switch back to the previous one.

---

## 10. Limits and quotas

These can be tuned in `.env`:

| Limit | What you see | Setting (default) |
|-------|--------------|-------------------|
| **Per-account concurrency** | One LLM task per account at a time; a second submission is rejected outright — wait for the first to finish | `LLM_MAX_CONCURRENCY_PER_USER` (1) |
| **Global concurrency** | At most 3 tasks site-wide; the rest **queue up**, the UI shows "queued", and your turn resumes automatically | `LLM_MAX_CONCURRENCY` (3), `LLM_QUEUE_SIZE` (20), `LLM_QUEUE_TIMEOUT_SECONDS` (180) |
| **Daily token quota** | Counted per calendar day; once exhausted, no more generation that day | `DAILY_TOKEN_QUOTA` (0 = unlimited), `QUOTA_RESET_UTC_OFFSET_HOURS` (8, i.e. the day rolls over in UTC+8), `QUOTA_EXEMPT_ADMINS` (true) |

Other boundaries you will actually hit:

- 30,000 characters of body text per document (`DOC_MAX_CHARS`); anything beyond is truncated and flagged in the UI;
- At most 5 clarification rounds (the `MAX_CLARIFICATION_ROUNDS` constant in code, not an `.env` setting);
- Each LLM call has a timeout (`LLM_TIMEOUT_SECONDS`, 120s by default) — split very large documents by module.

> Admins can check token spend per account for the last N days via `GET /api/limits/usage?days=N`; any user can check their own remaining quota via `GET /api/limits/status`.

---

## 11. FAQ

**Q: Upload fails with "Only .docx and .pdf files are supported"**
Requirements docs accept `.docx` and `.pdf` only. Save `.doc` as `.docx` first. Test mindmaps accept `.md` (Markdown outline) only.

**Q: A scanned PDF produces almost no content**
PDF parsing extracts text; a pure image scan has none to extract. Use a PDF with selectable text, or upload the Word file instead.

**Q: Lark import fails or hangs at "validating the Lark link"**
Lark import depends on `lark-cli` and an authorised identity on the server, which the deployment must configure. The UI offers a retry button; if it keeps failing, contact the admin or export the Lark doc to Word and upload that.

**Q: Do I have to answer the clarification questions? Can I skip them?**
Clarification is where quality comes from. If the document is already unambiguous, the system reports no open questions and goes straight to generation; otherwise **answering properly pays for itself** — skipping buys cases you'll have to rework.

**Q: The case ID prefix is wrong**
The prefix comes from the **module's English name**. Change it under Knowledge Base → Modules, or pick / create the right module on the next module confirmation card.

**Q: The page says "queued", or my request is rejected**
See [section 10](#10-limits-and-quotas). Wait a moment and retry — don't hammer the button.

**Q: I lost connection / refreshed the page — is my progress gone?**
No. Sessions, documents, clarification history and generated cases all live on the server; reopen the session and continue. If a streaming task was cut off, retry as the UI suggests.

**Q: Is my data isolated?**
It is isolated **per project**, and members of a project share its sessions, cases and knowledge base. The public instance is a **shared demo environment** — please do **not** upload confidential documents or customer data.

---

## 12. Getting more out of it

1. **Feed one module's requirements at a time.** Splitting a sprawling PRD across several runs gives more accurate module assignment and steadier case granularity.
2. **Answer clarifications with concrete values.** "30s timeout, then 2 retries" beats "handle it the usual way" by a mile.
3. **Always write a reason when you dislike.** One line — "missed the concurrency scenario" — can become a Skill that applies to that module from then on.
4. **Edit rather than regenerate.** Fixing the table in place teaches the system what was off; a wholesale regeneration teaches it nothing.
5. **Clean the knowledge base regularly.** Stale product rules keep polluting every later generation.
6. **Upload the PRD and the mindmap together.** When you have prior test thinking, this is the fastest way to raise coverage.

---

## Contact

Questions or ideas after trying it out:

- GitHub issues: <https://github.com/mystufo/caseweave/issues>
- WeChat: **mystufo** (please mention "CaseWeave" in the request)
- Email: **mystufo@aliyun.com**
