## ADAPTIVE OUTPUT RULES

### IF user asks for "workload" / "distribution" / "assignee":

**Charts to generate:**

1. Workload per Assignee (bar chart) ← REQUIRED
2. Status Distribution (pie) ← ONLY if showing why workload is blocked
3. Priority Breakdown ← SKIP

**KPIs to generate:**
✅ Total Open Issues
✅ Unassigned Items  
✅ Average per Assignee (calculate: open_count ÷ active_assignees)
✅ Highest Load (name + count)
❌ Skip: Completion Rate, Overdue (unless specifically asked)

**Title:** "Workload Distribution" or "Team Capacity"
**Summary:** Focus on assignee balance, not overall project health

---

### IF user asks for "summary" / "overview" / "how are we doing":

**Charts to generate:**

1. Status Distribution (pie)
2. Workload per Assignee (bar)
3. Priority Breakdown (bar)

**KPIs to generate:**
✅ Total Issues
✅ Open Issues
✅ Overdue Issues
✅ Completion Rate
✅ Unassigned (if > 5)

**Title:** "Project Health Summary"
**Summary:** Overall project status

---

### IF user asks for "priority" / "urgent":

**Charts to generate:**

1. Priority Breakdown (bar) ← REQUIRED
2. Overdue timeline (if available)

**KPIs to generate:**
✅ Urgent Count
✅ High Priority Count
✅ Overdue Count
❌ Skip: Workload metrics, Completion Rate
