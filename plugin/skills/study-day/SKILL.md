---
name: study-day
description: >
  This skill should be used when the student types /study-day or asks to "start my study day",
  "what should I study today" or "pick something from my plan", then works on one plan item
  and logs the minutes on it.
---

# Start a study day

Use the studysystem tools. Every hour counts as plan use only when it is logged on a chapter,
topic or setup item shown in the plan in force when the session started (D-113), so get the plan
first.

1. Call `study_get_plan` and show the student every item it returns, lane by lane, with its
   reason. Show the study lane as its `entries`: one per chapter, naming the chapter and its
   topics, or one per topic with no chapter, marked "not taught yet". Each topic's reason is in
   the `items` with its topic_id. Ask which one they take.
2. Start the work on the item they pick:
   - A study chapter: ask "explain or practice?". For explain, stop here and tell the student to
     run `/explain-chapter`. For practice, before any work, call `study_start_session` with mode
     `practice`, subject_type `material` and subject_id the chapter's material_id.
   - A study topic with no chapter: before any work, call `study_start_session` with mode
     `practice`, subject_type `topic` and subject_id its topic_id.
   - A review item: before any work, call `study_start_session` with mode `review-quiz`,
     subject_type `topic` and subject_id the item's topic_id.
   - A setup item: start no session; say what it asks for and do it with the student.
   - A first-material item is the exception: stop here and tell the student to run
     `/add-chapter` - it adds the chapter, makes its note, links it to its topics and logs the
     minutes on the chapter. `/explain-chapter` then teaches it.
3. Do the work with the student.
4. Log the minutes:

<!-- shared:log-step -->
When the student says they are done, ask how many minutes they studied. Never estimate them.
Log them with `study_log_hours`. Always pass the session_id if a session was started, even when
no item fits. Also pass the item's material_id (a study chapter), topic_id (a review item, or a
study topic with no chapter) or setup_key exactly as `study_get_plan` returned it; leave out all
three only if no shown item fits, and say so. If the student spent no time, log nothing.
<!-- /shared:log-step -->
