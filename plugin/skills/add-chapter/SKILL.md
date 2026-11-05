---
name: add-chapter
description: >
  This skill should be used when the student types /add-chapter or asks to "add a chapter",
  "feed a chapter" or "make the note for this PDF": it adds one chapter PDF to a course, makes
  its exam note and links it to the course's topics. It does not teach the chapter.
---

# Add a chapter

Use the studysystem tools. The student may type the course code and the PDF path after the
command; ask for whichever is missing. The path is the full Windows path (in Explorer: select
the file, Ctrl+Shift+C). The studysystem server runs on the student's computer and reads the
file itself, so no folder needs to be connected for this.

Do not call `study_get_plan` and do not ask the student to pick a plan item. Adding a chapter
does not depend on the plan, so start the work as soon as you have the course and the path.

1. Add the chapter with `study_add_material` (kind `chapter` unless it is clearly slides).
2. Make the note:

<!-- shared:make-note-step -->
Make its exam note: `study_start_generation` kind `note` with the material_id. Fetch every
part, then call `study_submit_generation` until it reports no errors (at most 3 submissions).
<!-- /shared:make-note-step -->

   If the material already existed and its note is refused as done (`already_noted`), say so
   - the chapter is ready to explain - and go on to step 3.
3. Link it to its topics:

<!-- shared:tag-step -->
Link the chapter to the topics it teaches: `study_start_generation` kind `tagging` with the
material_id. Fetch every part, then call `study_submit_generation` until it reports no errors
(at most 3 submissions). Show the student which topics it linked. A course with no active topic
yet is refused: say so - its topics come first from its past papers, or from its syllabus
when it has none. A note makes a chapter count as taught, but the plan lifts a topic only
once it is linked to a taught chapter.
<!-- /shared:tag-step -->

4. Do not teach the chapter here. Tell the student to run `/explain-chapter` when they want it
   taught.
5. Log the minutes. This command shows no plan items, so pass this chapter's material_id:

<!-- shared:log-step -->
When the student says they are done, ask how many minutes they studied. Never estimate them.
Log them with `study_log_hours`. Always pass the session_id if a session was started, even when
no item fits. Also pass the item's material_id (a study chapter), topic_id (a review item, or a
study topic with no chapter) or setup_key exactly as `study_get_plan` returned it; leave out all
three only if no shown item fits, and say so. If the student spent no time, log nothing.
<!-- /shared:log-step -->
