import random

from bot.services.distribution import Candidate, DutySpec, distribute, pick, score

BALLS = DutySpec(1, requires_car=True, sort_order=10)
WATER = DutySpec(2, requires_car=False, sort_order=20)
BIBS = DutySpec(3, requires_car=False, sort_order=30)
LAUNDRY = DutySpec(4, requires_car=False, sort_order=40)
ALL = [BALLS, WATER, BIBS, LAUNDRY]


def cand(uid, car=False, counts=None, busy=False):
    counts = counts or {}
    return Candidate(uid, car, counts, sum(counts.values()), busy)


def test_example_from_spec():
    # Таблица из ТЗ. Даниял: вода 2, мячи 5, манишки 1; Арман: мячи 1, манишки 3, стирка 1; Тимур: вода 1.
    daniyal = cand(1, car=True, counts={2: 2, 1: 5, 3: 1})
    arman = cand(2, counts={1: 1, 3: 3, 4: 1})
    timur = cand(3, car=True, counts={2: 1})
    rng = random.Random(0)
    # Мячи — Тимуру: есть машина и ни разу не возил.
    assert pick(BALLS, [daniyal, arman, timur], {}, rng).user_id == 3
    # Вода по формуле score = 3*вода + всего: Даниял 6+8=14, Арман 0+5=5, Тимур 3+1=4.
    # Тимур почти ничего не делал, поэтому общая нагрузка перевешивает.
    assert pick(WATER, [daniyal, arman, timur], {}, rng).user_id == 3
    # Если Тимур уже занят мячами на этой игре — вода Арману (как в примере ТЗ).
    assert pick(WATER, [daniyal, arman, timur], {3: 1}, rng).user_id == 2
    result, _ = distribute(ALL, [daniyal, arman, timur], rng)
    assert result[BALLS.id] == 3 and result[WATER.id] == 2


def test_balls_only_for_car_owners():
    cands = [cand(1), cand(2), cand(3, car=True, counts={1: 10})]
    result, unassigned = distribute(ALL, cands, random.Random(1))
    assert result[BALLS.id] == 3
    assert unassigned == []


def test_no_car_leaves_balls_unassigned():
    cands = [cand(i) for i in range(1, 6)]
    result, unassigned = distribute(ALL, cands, random.Random(1))
    assert BALLS.id not in result
    assert unassigned == [BALLS.id]
    assert len(set(result.values())) == 3


def test_one_duty_per_person_when_enough_players():
    cands = [cand(i, car=i == 1) for i in range(1, 8)]
    result, _ = distribute(ALL, cands, random.Random(3))
    assert len(set(result.values())) == len(result) == 4


def test_multiple_duties_when_few_players():
    cands = [cand(1, car=True), cand(2)]
    result, unassigned = distribute(ALL, cands, random.Random(3))
    assert unassigned == []
    assert sorted(result.values()) == [1, 1, 2, 2]


def test_car_owner_reserved_for_balls():
    # Единственный водитель — наименее нагруженный, но мячи распределяются первыми.
    cands = [cand(1, car=True), cand(2, counts={2: 1}), cand(3, counts={3: 1}), cand(4, counts={4: 1}), cand(5, counts={2: 3})]
    result, _ = distribute(ALL, cands, random.Random(0))
    assert result[BALLS.id] == 1
    assert 1 not in [result[WATER.id], result[BIBS.id], result[LAUNDRY.id]]


def test_score_formula_and_recent_penalty():
    c = cand(1, counts={2: 2, 3: 1}, busy=True)
    assert score(c, 2) == 2 * 3 + 3 + 2
    fresh = cand(2, counts={2: 1, 3: 2})
    # у fresh та же сумма и меньше этой обязанности, без штрафа за прошлую игру
    assert pick(WATER, [c, fresh], {}, random.Random(0)).user_id == 2


def test_fairness_over_many_games():
    """За много игр нагрузка распределяется почти поровну."""
    rng = random.Random(7)
    players = [cand(i, car=i <= 3) for i in range(1, 11)]
    for _ in range(50):
        attending = rng.sample(players, 8)
        result, _ = distribute(ALL, attending, rng)
        for duty_id, uid in result.items():
            p = next(x for x in players if x.user_id == uid)
            p.duty_counts[duty_id] = p.duty_counts.get(duty_id, 0) + 1
            p.total += 1
    # Без машины — почти поровну.
    walkers = [p.total for p in players[3:]]
    assert max(walkers) - min(walkers) <= 3
    # Водители возят мячи почти каждую игру, поэтому прочих обязанностей у них меньше.
    for driver in players[:3]:
        others = driver.total - driver.duty_counts.get(BALLS.id, 0)
        assert others < min(walkers)
    # Мячи достаются только водителям и примерно поровну между ними.
    balls = [p.duty_counts.get(BALLS.id, 0) for p in players]
    assert sum(balls[3:]) == 0
    assert max(balls[:3]) - min(balls[:3]) <= 2


def test_hungarian_matches_brute_force():
    from itertools import permutations

    from bot.services.distribution import hungarian

    rng = random.Random(5)
    for _ in range(200):
        n = rng.randint(1, 4)
        m = rng.randint(n, 6)
        cost = [[rng.randint(0, 20) for _ in range(m)] for _ in range(n)]
        cols = hungarian(cost)
        assert len(set(cols)) == n
        best = min(sum(cost[i][p[i]] for i in range(n)) for p in permutations(range(m), n))
        assert sum(cost[i][cols[i]] for i in range(n)) == best


def test_no_repeat_of_same_duty_next_game():
    for seed in range(30):
        rng = random.Random(seed)
        players = [cand(i, car=True) for i in range(1, 5)]
        first, _ = distribute(ALL, players, rng)
        for duty_id, uid in first.items():
            p = players[uid - 1]
            p.duty_counts[duty_id] = 1
            p.total += 1
            p.busy_last_game = True
        second, _ = distribute(ALL, players, rng)
        assert all(first[d] != second[d] for d in first), seed
