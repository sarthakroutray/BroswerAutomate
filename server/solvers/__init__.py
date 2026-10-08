"""
server.solvers — Structured solver loops for high-volume, repeatable tasks.

The generic browser_run_task loop in server.agent.orchestrator is general-
purpose but loses accuracy on tasks with a known structure: quizzes always
have "find questions → pick answers → click Next → repeat → submit", and
coding challenges always have "extract problem → write code → compile →
fix errors → submit".

These solvers consume the structured extraction (EXTRACT_PAGE_CONTEXT) and
drive the LLM in tight, focused sub-loops — no screenshot, no full DOM
dump — which gives substantially better accuracy at much lower cost.

Public API:
  async def solve_quiz(tab, llm, *, max_questions=50, confidence_threshold=0.6, allow_unsafe=False) -> dict
  async def solve_coding(tab, llm, *, max_attempts=5, allow_unsafe=False) -> dict
"""