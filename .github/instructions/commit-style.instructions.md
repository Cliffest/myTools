---
description: "Bilingual Conventional-Commit rules"
applyTo: "**"
---

# When Copilot generates a commit message, follow **all** of these rules  

1. **Title line**  
   * Format `<type>(<scope>): <summary>` using Conventional Commits.  
   * `type` in lowercase: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `build`, `ci`.  
   * `scope` is optional but recommended; keep it lowercase.  
   * `<summary>` ≤ 72 chars, imperative mood, English, no final period.

2. **Blank line** after the title.

3. **English bullet list** (one “- ” per logical change)  
   * Start each bullet with a capital letter, present-tense verb.  
   * No trailing period, keep lines ≤ 120 chars.

4. **Blank line**, then **matching Chinese bullet list** in the same order.  
   * Use “- ” prefix again, don’t add punctuation.  
   * The English and Chinese lists must have the same number of bullets.

5. Don’t add issue numbers, emoji, author names or timestamps; those are handled elsewhere.

Example
```
chore(cleanup): remove test/example scripts and debug logs

- Delete all test and example scripts for a cleaner codebase
- Remove unnecessary console.log and debug output from core modules

- 删除所有测试和示例脚本，提升代码整洁度
- 清理主模块中的多余调试输出
```
