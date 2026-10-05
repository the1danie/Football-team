"""Справедливое распределение обязанностей.

Чистые функции без БД, чтобы алгоритм было легко тестировать.

score = (сколько раз игрок выполнял эту обязанность) * 3
      + (сколько всего обязанностей у игрока)
      + 2, если у игрока была обязанность на прошлой игре
      - 10 × (действующие минусы за неответ на опрос)

Чем меньше score, тем выше приоритет. Все обязанности игры распределяются
разом так, чтобы суммарный score был минимальным (задача о назначениях,
венгерский алгоритм) — это не даёт «последней» обязанности достаться тому,
кто делал её в прошлый раз. Второй раз за игру игрок получает обязанность,
только если участников меньше, чем обязанностей.
"""

import random
from dataclasses import dataclass, field

DUTY_WEIGHT = 3
RECENT_PENALTY = 2
EXTRA_DUTY_COST = 10_000  # цена второй (третьей…) обязанности одному человеку за игру
INFEASIBLE = 10**9


@dataclass
class Candidate:
    user_id: int
    has_car: bool
    duty_counts: dict[int, int] = field(default_factory=dict)  # duty_id -> сколько раз выполнял
    total: int = 0
    busy_last_game: bool = False
    # Действующие минусы × вес (config.penalty_priority): такие игроки получают обязанности первыми.
    penalty_bonus: float = 0


@dataclass(frozen=True)
class DutySpec:
    id: int
    requires_car: bool
    sort_order: int = 0


def score(candidate: Candidate, duty_id: int) -> float:
    return (
        candidate.duty_counts.get(duty_id, 0) * DUTY_WEIGHT
        + candidate.total
        + (RECENT_PENALTY if candidate.busy_last_game else 0)
        - candidate.penalty_bonus
    )


def eligible(duty: DutySpec, candidates: list[Candidate]) -> list[Candidate]:
    return [c for c in candidates if c.has_car or not duty.requires_car]


def pick(
    duty: DutySpec,
    candidates: list[Candidate],
    load: dict[int, int],
    rng: random.Random | None = None,
) -> Candidate | None:
    """Выбрать исполнителя для одной обязанности.

    load — сколько обязанностей у игрока уже есть на этой игре.
    """
    rng = rng or random.Random()
    pool = eligible(duty, candidates)
    if not pool:
        return None
    # Случайный ключ последним — чтобы при равенстве не выбирался всегда один и тот же.
    keyed = [(load.get(c.user_id, 0), score(c, duty.id), rng.random(), c) for c in pool]
    keyed.sort(key=lambda t: t[:3])
    return keyed[0][3]


def distribute(
    duties: list[DutySpec],
    candidates: list[Candidate],
    rng: random.Random | None = None,
) -> tuple[dict[int, int], list[int]]:
    """Распределить обязанности.

    Возвращает ({duty_id: user_id}, [duty_id без исполнителя]).
    """
    rng = rng or random.Random()
    ordered = sorted(duties, key=lambda d: (d.sort_order, d.id))
    fillable = [d for d in ordered if eligible(d, candidates)]
    unassigned = [d.id for d in ordered if d not in fillable]
    if not fillable:
        return {}, unassigned

    # Каждому кандидату — по «слоту» на каждую обязанность: слот k означает,
    # что это его (k+1)-я обязанность на игре. Небольшой шум — для случайного
    # выбора среди равных.
    slots = [(c, k) for k in range(len(fillable)) for c in candidates]
    noise = {c.user_id: rng.random() * 0.1 for c in candidates}
    cost = [
        [
            INFEASIBLE
            if d.requires_car and not c.has_car
            else k * EXTRA_DUTY_COST + score(c, d.id) + noise[c.user_id]
            for c, k in slots
        ]
        for d in fillable
    ]
    columns = hungarian(cost)
    result = {d.id: slots[col][0].user_id for d, col in zip(fillable, columns, strict=True)}
    return result, unassigned


def hungarian(cost: list[list[float]]) -> list[int]:
    """Минимальное назначение строк столбцам (строк ≤ столбцов).

    Возвращает номер столбца для каждой строки. O(n²·m).
    """
    n, m = len(cost), len(cost[0])
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)  # p[j] — строка, назначенная столбцу j (1-based, 0 — нет)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    result = [0] * n
    for j in range(1, m + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result
