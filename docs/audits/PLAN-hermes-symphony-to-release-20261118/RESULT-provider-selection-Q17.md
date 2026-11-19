# Q17 · Выбор провайдера для пилота Hermes Symphony

**Дата исследования:** 2026-11-19. **Статус:** рекомендация для CR-H, не допуск к запуску.  
**Основание:** [исходный handoff Q17](./HANDOFF-provider-selection-Q17.md) и независимый разбор ChatGPT Pro по публичным источникам. Локальные свойства транспорта взяты из handoff; облачные аккаунты, квоты, образы и MCP-вызовы не проверялись. ВМ не создавались, расходов нет.

## Решение

| | Основной кандидат | Условный резерв |
|---|---|---|
| Провайдер и регион | Azure North Europe (`northeurope`) | AWS EC2 Stockholm (`eu-north-1`) |
| ВМ | `Standard_D4as_v5`, 4 vCPU / 16 GiB | `m6i.xlarge`, 4 vCPU / 16 GiB |
| ОС | Windows 11 Enterprise 25H2 x64, single-session, Gen2; `MicrosoftWindowsDesktop:Windows-11:win11-25h2-ent:<version>` | Windows Server 2025 English Full Base x64/UEFI, License Included; семейство `Windows_Server-2025-English-Full-Base` |
| Постоянный диск и доступ | Premium SSD P10 128 GiB, `Premium_LRS`, статический IPv4; RDP только с разрешённого IP | EBS gp3 100 GiB, обычный IPv4 на время работы; RDP с ограниченным источником или через EC2 Instance Connect Endpoint |
| Управление арендой | Официальные ARM MCP + Azure Compute MCP | Официальный managed AWS MCP, узкие EC2-операции через IAM |
| Главный стоп-фактор | Не подтверждены лицензионные права владельца на Windows 11 в Azure | MXC с точным профилем проекта на Windows Server не подтверждён; резерв только для диагностики |

Зафиксировать точные версии образа Azure и региональный AMI ID AWS до возможного запуска. По документации нет предложения, которое одновременно доказывает подходящую лицензию Windows 11, полный рабочий MCP lifecycle на конкретном аккаунте и совместимость MXC. GPU не нужен. 16 GiB — ориентир на одну активную попытку; 4 GB свободной памяти перед стартом — нижний порог, не гарантия доступности всего лимита Job 6 GB / 64 процесса.

### Лицензирование

У Azure первым проверяется право использовать Windows 11 для фактической нагрузки. Microsoft перечисляет подходящие Microsoft 365, Windows Enterprise/VDA и Education пер-пользовательские подписки. Для dev/test существуют отдельные условия Visual Studio и ограниченные исключения для Student/Free Trial; нельзя считать их применимыми без проверки. `licenseType=Windows_Client` заявляет право, а не покупает лицензию. [Microsoft: Windows 11 на Azure](https://learn.microsoft.com/en-us/azure/virtual-machines/windows/windows-desktop-multitenant-hosting-deployment).

В AWS стоимость Windows Server включена. RDP предусмотрен для настройки и диагностики, а не обещания полноценного многопользовательского рабочего стола.

## Стоимость

Снимок публичных On-Demand/PAYG ставок в USD, до налогов. Для сравнения месячные дисковые ставки переведены через 730 ч. Трафик, snapshots, дополнительные сервисы и модельные расходы не включены. `L` — отдельная стоимость прав Windows 11, если их ещё надо приобрести; не считать её нулём или пропорциональной часам. Налог не установлен.

| Профиль | Вычисления | Диск | IPv4 | 2 ч | 48 ч | 730 ч |
|---|---:|---:|---:|---:|---:|---:|
| Azure | $0.192/ч | $19.71/мес. | $0.005/ч | **$0.44 + L** | **$10.75 + L** | **$163.52 + L** |
| AWS | $0.388/ч, включает $0.184/ч Windows | $8.36/мес. | $0.005/ч | **$0.81** | **$19.41** | **$295.25** |

Azure: в расчёте нового статического IPv4 первый час не тарифицируется; для уже удерживаемого IP считать все часы. Учёт compute поминутный, диска почасовой. В `Deallocated` остаются диск и удерживаемый IP (~$23.36 за 730 ч), в `Stopped` compute может оплачиваться. AWS: compute On-Demand посекундно, минимум 60 секунд после каждого старта; при `stopped` начисляется хранение EBS, обычный IPv4 освобождается, Elastic IP продолжает тарифицироваться. Краткая аренда не означает бесплатное хранение или удаление остатков.

Источники: [Azure Retail Prices: ВМ](https://prices.azure.com/api/retail/prices?%24filter=serviceName+eq+%27Virtual+Machines%27+and+armRegionName+eq+%27northeurope%27+and+armSkuName+eq+%27Standard_D4as_v5%27+and+priceType+eq+%27Consumption%27), [Azure Retail Prices: P10](https://prices.azure.com/api/retail/prices?%24filter=serviceName+eq+%27Storage%27+and+armRegionName+eq+%27northeurope%27+and+meterName+eq+%27P10+LRS+Disk%27+and+priceType+eq+%27Consumption%27), [Azure IP pricing](https://azure.microsoft.com/en-us/pricing/details/ip-addresses/), [AWS regional EC2 price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/eu-north-1/index.csv) (Windows SKU `HWTWAM8PS6K5PD6F`, `RunInstances:0002`, Shared, OnDemand; снимок 2026-11-18 22:02:54 UTC), [Amazon VPC pricing](https://aws.amazon.com/vpc/pricing/).

## MCP: что подтверждено документацией и что проверять

- **Azure.** Применять один проверяемый ARM-шаблон через `whatif_deployment` / `create_deployment` с закреплённым образом, `Windows_Client`, Trusted Launch/Secure Boot/vTPM, постоянным диском, сетью и delete options. В специализированной Azure Compute MCP `vm create` не хватает `licenseType`, хотя `vm update` его принимает. Для статуса, start/restart/deallocate/delete использовать Compute MCP и ждать подтверждения провайдера; проверить instance view и каждый платный ресурс при освобождении. ARM MCP в public preview; не предполагать доступность операций в конкретном tenant и не приписывать ARM MCP универсальный `delete_resource`. [ARM MCP](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/azure-resource-manager-mcp-overview), [Azure Compute MCP](https://learn.microsoft.com/en-us/azure/developer/azure-mcp-server/tools/azure-compute).
- **AWS.** В современном официальном каталоге `aws___run_script` и `aws___get_tasks`; старый AWS API MCP использовал `call_aws`. Закрепить endpoint и реальный `tools/list`; ограничить EC2-операции IAM. Нужны Run/Describe/Start/Reboot/Stop/Terminate, устойчивый `ClientToken` для создания, проверка конечного состояния, `DeleteOnTermination` и сверка EBS/IP. Успех задачи MCP не означает, что ВМ уже остановлена или что гостевая ОС перезагрузилась. [Каталог AWS MCP](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/understanding-mcp-server-tools.html), [EC2 RunInstances](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_RunInstances.html).
- **Отложены:** у [GCP Compute MCP](https://docs.cloud.google.com/compute/docs/reference/mcp/tools_list/create_instance) есть lifecycle, но опубликованный create не принимает нужные настройки диска/сети/маркировки и закреплённый image ID. У Vultr официальный MCP read-only, не годится для аренды. Самописный мост не нужен по умолчанию ни для Azure, ни для AWS; вернуться к нему только если выявится конкретный пробел.

Контроллер аренды остаётся вне арендуемого worker. В CR-H ограничить каталог, регион, скоуп IAM/RBAC и максимум одной ВМ; не передавать модели широкие полномочия или секреты. Записывать lease ID и намерение до create, хранить инвентарь ВМ/дисков/IP и отдельные сроки compute и хранения. Не повторять create при неизвестном исходе до сверки. Различать завершение MCP-вызова, состояние ресурса, boot ID гостя и состояние попытки. TTL должен работать независимо от worker; окончание начислений по ресурсам подтверждается readback. Политику сохранения результатов и удаления определить до аренды.

## Передача в CR-H: минимальный проверочный этап

1. **До заказа:** подтвердить право на ОС, цену/налог, региональную квоту, неизменяемый образ, точный каталог MCP и правила сохранения/освобождения ресурсов. Получить отдельное согласие на запуск и расходы.
2. **Одна ВМ, максимум 2 часа на первый зонд:** проверить ОС/build и постоянный диск, окно обновлений, Codex CLI 0.167.0, свободную RAM. Использовать синтетические данные, без рабочих репозиториев, авторизации Codex и запросов к модели. Ориентир для согласования: **до $2 на инфраструктуру**, до налогов и отдельной лицензии; это не жёсткий лимит провайдера.
3. **Model-free проверка:** явный `windows.sandbox=mxc` и точный проектный permission profile с managed config, затем положительные и отрицательные read/write проверки. `prefer_mxc` с возможным legacy fallback не считается доказательством. Документация называет Windows 11 24H2 build 26100.9278 и 25H2 build 26200.9278, но не подтверждает этот сценарий на Windows Server. [OpenAI: Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox).
4. **Перед рабочим пилотом:** проектные fixtures, `doctor`, CR-4 для остановки дерева MXC/Job Object при отказах, проверка нового boot ID и сохранности диска после restart и stop/start. Отдельно подтвердить полный MCP lifecycle до cleanup на одноразовых данных.
5. **Stop:** нет прав/квоты — не создавать. Explicit MXC не работает, нарушены границы, не хватает памяти, истёк TTL или CR-4 не пройден — не запускать рабочие попытки. Не уложились в два часа — `INCOMPLETE`, не PASS; вывести ВМ из вычислений и сверить состояние. Если подтверждения нет, оставить `UNKNOWN`, не заявлять успешную остановку. Не подменять MXC legacy sandbox или Linux.

Если первый этап пройден, ориентир для отдельного согласования пилота: 48 дополнительных часов аренды после двухчасового зонда и не более 24 часов удержания диска/IP после окончания compute. Оценка Pro для такого окна: около $11.97 + права Windows в Azure либо $20.50 в AWS; предложенный ориентир $25 на инфраструктуру не включает лицензию, налог, трафик и модель и не является утверждённым бюджетом.

**Следующее решение исходной сессии:** сформировать CR-H для Azure при подтверждённом entitlement; если права не подтверждаются, не развёртывать Windows 11. AWS допускать к обсуждению только как отдельно согласованный model-free эксперимент с Windows Server, не как готовый production worker.
