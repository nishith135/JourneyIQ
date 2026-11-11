
# JourneyIQ — Agent Instructions

## 1. Source of Truth
- docs/PRD.md defines product requirements.
- docs/SUCCESS_CRITERIA.md defines acceptance criteria.
- docs/BUSINESS_RULES.md defines domain behaviour.
- docs/DATA_DICTIONARY.md defines the approved data model.
- docs/ARCHITECTURE.md defines technical architecture.
- Approved feature specifications define implementation scope.

## 2. Rules
- Never invent missing requirements or business rules.
- Never silently resolve conflicting documentation.
- Ask for clarification when requirements are ambiguous.
- Do not change approved decisions without explicit approval.
- Do not implement features outside the current approved scope.
- Do not assume that a feature is complete because code exists.

## 3. Implementation
- Work on one approved feature at a time.
- Identify relevant requirement IDs before implementation.
- Inspect existing code before modifying it.
- Explain the implementation approach before substantial changes.
- Write tests for acceptance criteria, edge cases and failures.
- Verify tests before reporting completion.

## 4. Reporting
At the end of each task, report:
- Requirements addressed
- Files changed
- Tests executed and actual results
- Known limitations
- Unresolved questions
- Any assumptions (which must be approved before implementation)
