# Пакет независимой проверки Hermes → Symphony / Codex

Корректирующая дельта после review: `docs/audits/CR-hermes-symphony-ingress-offline-20261106/cancel-recovery-correction.md`. Старая recovery проверяла reopen БД при том же живом LivePort и OS handles; controller restart не доказан. Исправленный verifier по умолчанию читает immutable ebcc60d2 Git objects; `--root` принимает только исходный опубликованный export с pinned hashes, а не изменённый candidate worktree. Старые manifests и live artifacts сохранены.

База реализации — `369a012cc9b5caece4b1a6e37fdecb1ca04530b3`, существующая ветка `codex/symphony-pm-offline-20261106`. Публикация добавляет task-local harness и доказательства; исходные adapter/plugin/offline-owner остаются прежними.

Начать с `approved-retry-1/REPORT.md` и `approved-retry-1/summary.json`. Точные request/job/attempt/process/thread identities, команды и OS snapshots находятся в `approved-retry-1/attempt-1/`, `attempt-2/`, `events.jsonl`, `ledger-readback.json`.

Без запуска модели, сети или изменений файлов:

```text
python -B .artifacts/live-smoke-20261106/verify_evidence.py
```

Проверка использует только Python stdlib: проверяет SHA256 всех 46 файлов исходного evidence manifest, JSON против synthetic input, два новых native starts, единственный cancel, lost response и два replay, temporal ordering, terminal observer и сохранённый ledger. Успех означает согласованность записанных доказательств; он не заменяет независимое повторение live событий.

Статусы: offline targeted campaign **144/144 PASS**; live read-only JSON **PASS**; локальная остановка дерева и cancel dedupe **PASS**; общая приёмка cancel **HOLD**. Billing/external settlement **UNKNOWN**, reservation удержана. Recovery проверен как SQLite connection + port reopen при сохранённом controller, не как полный restart controller или power-loss.

Два новых запуска разрешены явным допуском на повтор и третью суммарную попытку для отмены. В истории сохранён первый неудачный config attempt. Суммарно 3 native attempts, автоматических повторов 0. `run`/`continue-unstarted`/`retry_campaign.py run` — архив orchestration, не команда для ревью: допуск исчерпан, повторный worker запуск требует новой команды владельца. Native cancel здесь — Windows `TerminateJobObject`, не подтверждение остановки серверной inference.

Реальное выполнение использовало Codex CLI 0.167.0, kit 0.45.4 / HEAD `048d446ba484d5d3ca8b052210dbeed3e18d7fb8`, `gpt-6.1-sol`, reasoning High, текущий Windows. Внешний Codex driver имеет SHA256 `1b88924648b442675749890b726668790f8c7864707823e06f543acdd0cbe176`.

Исходники приватного kit и frozen S2, credentials, SQLite binary, временные Python/Node environments, пользовательские runtime directories, чужие untracked файлы и старые ZIP не публикуются. Вместо SQLite binary опубликован полный synthetic ledger readback. Frozen snapshot manifest опубликован для проверки точной версии; полный прогон source-contract тестов требует отдельно предоставленного приватного snapshot с совпадающими 21 sizes/hashes. Такой зависимости нет у `verify_evidence.py`.

Целевые тесты выполнялись canonical `scripts/run_tests.sh`; команды/env и UTF-16 исходные логи сохранены в `offline.ps1`, `regressions.ps1`, `offline.txt`, `regressions.txt`. Полный repository aggregate не заявлен зелёным. Исходники кита не менялись, frozen S2 не открывался флагами; Linear writes, push/merge production отсутствовали во время smoke. Текущая публикация review-ветки отдельно разрешена владельцем.

Артефакты сохраняют исходные машинные пути как execution evidence. Их наличие не означает, что указанные пути или временная projected schema доступны на машине ревьюера. Проверять следует относительные пути внутри пакета; live-команды нельзя слепо воспроизводить из этого журнала.
