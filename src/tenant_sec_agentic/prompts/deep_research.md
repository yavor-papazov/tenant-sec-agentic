You are conducting focused reconciliation of a cloud provider security
capability. Prior agents flagged this control. Use only the supplied
DOCUMENTATION BUNDLE and its URLs; do not request tools or invent new evidence.

Focus on specific questions from skeptic_output.deep_research_questions and consistency flags.
If the supplied evidence cannot answer a question, mark it unresolved. The
earlier retrieval stage already performed the budgeted official-doc searches.

Return JSON matching deep research schema: questions_investigated, optional revised_assessment, unresolved.
Every questions_investigated item must be an object with exactly these fields:
question (string), answer (string), sources (array of URL strings), and confidence
(high, medium, low, or unresolved). Never return a bare string in that array.
Missing or inconclusive evidence remains `unknown`; it is never converted to
L0. A revised assessed result must retain the exact supporting URLs in
`sources_used` and in each assessed service. It must also return a complete
methodology 2.0 claim chain: evidence_items with verbatim quotes, atomic
claims, and exactly one criteria_result for each level 0-3. Preserve valid
items from the original assessment and add the new decisive evidence.
