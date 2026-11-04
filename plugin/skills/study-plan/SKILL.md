---
name: study-plan
description: >
  This skill should be used when the student types /study-plan or asks to "make a new plan",
  "refresh my plan" or "rank everything again", so the dashboard shows a plan made from
  today's data.
---

# Make a new plan

The dashboard shows the latest saved plan and never makes one (D-163). This is the way to
refresh it outside a study flow.

1. Call `study_get_plan` once, using the studysystem tools.
2. Do not list its items here - the student reads them on the dashboard, which shows the same
   items.
3. Reply in one line: the plan is made, how many items each lane shows, and that the student
   should refresh the dashboard.
