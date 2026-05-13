# C Coding Style

Write C code in a concise, simple, and readable style inspired by BSD
style. Prefer direct code that is easy to inspect over broad abstractions or
clever shortcuts.

## Naming

- Use short, descriptive names that match the scope: `i` is fine for a loop;
  broader values need clearer names.
- Use lower-case names with underscores for functions, variables, files, and
  macros when practical.
- Keep type names and constants predictable; avoid project-private jargon when
  plain C terminology works.

## Indentation

- Indent with tabs for code blocks, following BSD-style C source layout.
- Keep wrapped lines aligned for readability and avoid excessive nesting.
- Leave whitespace where it helps separate related statements; do not pad code
  into columns that are hard to maintain.

## Braces and Control Flow

- Put opening braces for functions on their own line. Use compact, readable
  control blocks for `if`, `for`, `while`, and `switch`.
- Prefer early returns for error paths when they make the main path clearer.
- Always make ownership, lifetime, and fall-through behavior obvious.

## Functions and Files

- Keep functions focused and small enough to understand without scrolling much.
- Keep files organized around one clear responsibility.
- Prefer internal `static` helpers over exporting symbols that are not part of
  a real interface.

## Error Handling

- Check return values and handle failures close to where they occur.
- Return simple status values or `errno`-style errors when that fits the code.
- Clean up resources on every exit path; use a single cleanup label when it is
  clearer than duplicated cleanup.

## Comments

- Comment intent, invariants, and non-obvious decisions, not every statement.
- Keep comments current and concise.
- Remove misleading comments instead of preserving stale explanations.

## Includes and Headers

- Include the headers a file uses directly.
- Keep include lists minimal, ordered, and easy to scan.
- Put public declarations in headers only when another file needs them.

## Avoid Cleverness

- Prefer straightforward C programming over macros, hidden control flow, or
  unnecessary abstraction.
- Do not generalize code until there is a clear need.
- Optimize for readable maintenance first; optimize performance only with a
  concrete reason.
