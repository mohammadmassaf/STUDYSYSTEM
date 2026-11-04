---
name: explain-chapter
description: >
  This skill should be used when the student types /explain-chapter or asks to "explain a
  chapter", "teach me this chapter" or "start an explanation session": it teaches one chapter
  section by section with examples and drawn visuals, then logs the minutes. It adds the
  chapter and makes its note first if either is missing.
---

# Explain a chapter

Use the studysystem tools. The student may type the course code, the chapter's PDF path and the
note's markdown path after the command; ask for the course and the PDF path if missing. The
note path is needed only for a chapter noted in an earlier chat - no tool returns a note's text,
so such a note is read from its file.

1. Get the plan:

<!-- shared:plan-step -->
Call `study_get_plan` and show the student every item it returns, lane by lane. Keep the one
this chapter serves: a first-material setup item for this course, or a study or review topic
the chapter teaches. Ask the student to confirm it.
<!-- /shared:plan-step -->

2. Call `study_add_material` with the path (kind `chapter` unless it is clearly slides) to get
   the material_id. A chapter already added writes nothing and comes back `existing: true`.
3. Make sure it has a note:

<!-- shared:make-note-step -->
Make its exam note: `study_start_generation` kind `note` with the material_id. Fetch every
part, then call `study_submit_generation` until it reports no errors (at most 3 submissions).
<!-- /shared:make-note-step -->

   If it is refused with `already_noted`, the note was made in an earlier chat: read it from the
   note path, or ask the student for the path if they gave none. If the note was made in this
   step, this command does not link the chapter to its topics: at the end, tell the student to
   run `/add-chapter` on it, which links it.
4. Start the session: `study_start_session` mode `explain-chapter`, subject_type `material`,
   subject_id the material_id from step 2.
5. Teach the chapter section by section, in the order of its own pages or slides. A section is
   one idea, not one slide. For each section:
   - Start from a concrete example, then name the idea it shows.
   - Show it visually: a drawn diagram (an SVG or Mermaid picture, not boxes made of text
     characters), a table, or a run traced step by step.
   - Keep the words short: at most 2-3 sentences between examples and visuals. No long
     paragraphs, and no bullet summary of a slide.
   - Where the note marks an exam target, go deeper and show how the exam asks it.
   - End with one short question that checks the student understood, then stop and wait.
6. Log the minutes:

<!-- shared:log-step -->
When the student says they are done, ask how many minutes they studied. Never estimate them.
Log them with `study_log_hours`. Always pass the session_id if a session was started, even when
no item fits. Also pass the item's topic_id or setup_key exactly as `study_get_plan` returned
it; leave out both of those only if no shown item fits, and say so. If the student spent no
time, log nothing.
<!-- /shared:log-step -->
