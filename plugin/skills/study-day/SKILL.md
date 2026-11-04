---
name: study-day
description: >
  This skill should be used when the student types /study-day or asks to "start my study day",
  "what should I study today" or "pick something from my plan", then works on one plan item
  and logs the minutes on it.
---

# Start a study day

Use the studysystem tools. Every hour counts as plan use only when it is logged on a topic or
setup item shown in the plan in force when the session started (D-113), so get the plan first.

1. Call `study_get_plan` and show the student every item it returns, lane by lane, with its
   reason. Ask which one they take.
2. Start the work on the item they pick:
   - A review or study item: before any work, start a session on it with `study_start_session`
     - review -> mode `review-quiz`, study -> mode `practice`, both with subject_type `topic`
     and subject_id the item's topic_id.
   - A setup item: start no session; say what it asks for and do it with the student.
   - A first-material item is the exception: stop here and tell the student to run
     `/add-chapter` - it gets the plan again, adds the chapter, makes its note, links it to its
     topics and logs the minutes on this item. `/explain-chapter` then teaches it.
3. Do the work with the student.
4. Log the minutes:

<!-- shared:log-step -->
When the student says they are done, ask how many minutes they studied. Never estimate them.
Log them with `study_log_hours`. Always pass the session_id if a session was started, even when
no item fits. Also pass the item's topic_id or setup_key exactly as `study_get_plan` returned
it; leave out both of those only if no shown item fits, and say so. If the student spent no
time, log nothing.
<!-- /shared:log-step -->
