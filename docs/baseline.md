# Baseline перед рефакторингом

Дата: 2026-11-18

## Окружение

- ОС: Windows
- Python: 3.13.12
- Arcade: 3.3.3
- Ветка: `refactor/simplify`
- Коммит до рефакторинга: `00b3806` (тег `pre-refactor`)

## Юнит-тесты

```powershell
python -m unittest discover -s tests -p "test_engine.py" -q
```

Результат: `Ran 127 tests ... OK`.

## Стресс-тест: кэши и коллизии

```powershell
python tests/stress_test.py --scenario cache,collision `
  --cache-ops 100000 --pairs 100000 --duration 2 `
  --json docs/baseline.json
```

| Сценарий | Метрика | Значение |
| --- | --- | ---: |
| cache | `object_cache.set` | 296 167 ops/s |
| cache | `object_cache.get` | 381 561 ops/s |
| cache | `object_cache.set_many` | 86 071 ops/s |
| cache | `object_cache.get_many` | 168 561 ops/s |
| cache | `object_cache.dirty_cycle` | 107 083 ops/s |
| cache | `value_cache.set+get` | 207 750 ops/s |
| collision | `collision.transform` | 414 605 ops/s |
| collision | `collision.overlap` | 110 587 checks/s |
| collision | `collision.pipeline` | 50 162 pairs/s |

## Стресс-тест: живая регистрация и пакетное создание

```powershell
python tests/stress_test.py --scenario registration,batch `
  --objects 500 --timeout 60 --json docs/baseline_live.json
```

| Сценарий | Метрика | Значение |
| --- | --- | ---: |
| registration | `registration.objects` | 344.39 objects/s |
| registration | `registration.p50` | 2.843 ms/object |
| registration | `registration.p95` | 3.184 ms/object |
| registration | `registration.p99` | 3.678 ms/object |
| batch | `batch.objects` | 8 417.3 objects/s |
| batch | `batch.per_object` | 0.119 ms/object |

## Что важно проверить после этапов

1. Регистрация не должна ухудшиться после отказа от `Manager`; цель — p99 < 1 мс.
2. Пропускная способность `ObjectCache` не должна просесть после упрощения dirty-логики.
3. Коллизии не должны замедлиться после перехода на общий `World`.
4. Пакетное создание должно остаться не медленнее baseline.
5. Число потоков в sim-процессе должно стать равным одному.
6. После 100 000 циклов create/remove не должно расти число занятых слотов.

## Известные ограничения baseline

- Замеры сделаны на текущей машине; абсолютные значения нужны только для сравнения «до/после».
- `tact_counter` и реальный FPS окна в этот baseline не входят: для них нужен отдельный запуск с окном Arcade.
- Вывод стресс-теста в консоли Windows может отображаться в неверной кодировке; JSON-отчёт при этом корректен.
