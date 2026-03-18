# Skill Format Reference

Skills are Markdown files in `skills/` that extend the chat client with reusable
capabilities. They are invoked via `/<name>` or `/skill <name> [args]`.

---

## File Structure

```
skills/
├── my-skill.md           # Main skill file (required)
├── my-skill-ref.md       # Optional: loaded via context_files
└── SKILL_FORMAT.md       # This file
```

---

## SKILL.md Format

```markdown
---
name: skill-name
description: One sentence. What it does and when to use it.
spawn_agent: false
think_level: on
triggers: [keyword1, keyword2, phrase to watch for]
agent_tools: [web_search, read_file, bash]
context_files: [my-skill-ref.md]
---

Body content here. This is what the model reads and follows.

$ARGS is replaced with whatever the user typed after the skill name.
```

---

## Frontmatter Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | — | **Required.** Must match filename (minus `.md`). Lowercase, hyphens only. |
| `description` | string | — | **Required.** Shown in `/skills` listing. Describes when to use it. |
| `spawn_agent` | bool | `false` | If true, body becomes system prompt for a sub-agent that can use tools in a loop. If false, body is sent as a user message to the current session. |
| `think_level` | string | `on` | Thinking budget: `off`, `on`, or `deep`. Use `deep` for research and complex reasoning. |
| `triggers` | list | `[]` | Keywords shown in `/skills`. Future: used for auto-suggestion. |
| `agent_tools` | list | all | Limit which tools the sub-agent can use. Only relevant when `spawn_agent: true`. |
| `context_files` | list | `[]` | Additional `.md` files in `skills/` to append to the body at load time. |

---

## Body Content Guidelines

### For `spawn_agent: false` (inline mode)
The body is sent as a user message. Write it as a direct instruction or question.

```markdown
Run `git diff --staged` and summarise what's staged.

$ARGS
```

### For `spawn_agent: true` (agent mode)
The body becomes the system prompt for a sub-agent with tool access. Write it as
a role + protocol definition.

**Effective patterns for local 30B models:**

1. **State the role clearly in the first line.**
   - `You are an expert code reviewer with deep knowledge of C++ and Python.`

2. **Use numbered steps, not prose.** 30B models follow explicit sequences better than implicit instructions.
   ```
   1. Read the file with read_file.
   2. Check how the function is called with grep.
   3. Report issues ordered by severity.
   ```

3. **Include a Hard Rules section.** Small models need explicit NEVER/ALWAYS constraints.
   ```
   ## Hard Rules
   - NEVER report style issues as bugs.
   - ALWAYS include file:line references.
   ```

4. **Define the exact output format.** Never leave format to interpretation.
   ```
   ## Output Format
   - Summary: one sentence
   - Issues: numbered, file:line, problem, fix
   - Suggestions: optional
   ```

5. **Use `$ARGS`** to pass user input into the body.
   - `/review src/main.cpp` → `$ARGS` = `src/main.cpp`

---

## Examples

### Minimal inline skill
```markdown
---
name: explain
description: Explain the last piece of code in simple terms
spawn_agent: false
---
Look at the most recent code in our conversation and explain it as if I'm a
smart non-programmer. No jargon. Use an analogy if helpful. $ARGS
```

### Agent skill with tools
```markdown
---
name: audit
description: Security audit of a file or directory
spawn_agent: true
think_level: deep
agent_tools: [read_file, glob, grep, bash]
triggers: [security, audit, vulnerability, CVE]
---
You are a security-focused code auditor.

1. Read the target: $ARGS
2. Use glob/grep to understand the codebase context.
3. Look for: injection risks, hardcoded secrets, unvalidated input, unsafe dependencies.

## Hard Rules
- NEVER flag theoretical issues without a concrete exploit path.
- ALWAYS rate severity: Critical / High / Medium / Low.

## Output Format
### Summary
### Findings (ordered Critical → Low)
Each finding: location, issue, exploit scenario, fix.
### Clean Areas
```

### Multi-file skill using context_files
```markdown
---
name: deep-research
description: Research with extended reference material
spawn_agent: true
think_level: deep
context_files: [deep-research-protocol.md]
---
Follow the protocol in the appended reference. Topic: $ARGS
```

---

## Invocation

```
/research quantum computing breakthroughs 2024
/skill research quantum computing breakthroughs 2024   # equivalent
/skills                                                 # list all with triggers
```

Arguments after the skill name replace `$ARGS` in the body verbatim.
