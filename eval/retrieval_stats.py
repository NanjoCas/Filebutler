"""Statistics for the controlled retrieval evaluation; pure functions, no I/O.

- tie_aware: Recall@k and Success@k when files tie on score across the k-th place
  (expected values under a uniform random order of the tied files);
- random_floor: what a random ranking of N files would score;
- sign_test: exact two-sided binomial test on better/worse counts (no scipy here);
- cluster_bootstrap: CI of a mean of per-question deltas, resampling whole clusters
  (paraphrases of one information need are not independent);
- shapley / factorial: split the gain from a baseline to a target config among
  factors, per question, so the parts add up exactly to the total.
"""
import itertools
import math
import random


def tie_aware(ranked, gold, k):
    """ranked: [(path, score)] best first; gold: set of paths.
    Returns (recall, success, straddle): expected Recall@k and Success@k when files tied
    at the k-th score are ordered at random, and whether such a tie crosses the cutoff."""
    if not gold:
        return None, None, False
    if len(ranked) <= k:
        hits = sum(p in gold for p, _ in ranked)
        return hits / min(k, len(gold)), float(hits > 0), False
    cut = ranked[k - 1][1]
    above = [p for p, s in ranked if s > cut]
    tied = [p for p, s in ranked if s == cut]
    slots = k - len(above)
    sure = sum(p in gold for p in above)
    tied_gold = sum(p in gold for p in tied)
    expected = sure + tied_gold * slots / len(tied)
    if sure:
        success = 1.0
    else:  # at least one tied gold file among the slots drawn from the tie
        success = 1 - math.comb(len(tied) - tied_gold, slots) / math.comb(len(tied), slots)
    straddle = len(above) + len(tied) > k
    return expected / min(k, len(gold)), success, straddle


def random_floor(g, n, k):
    """Expected Recall@k and Success@k of a random order of n files with g relevant."""
    if not g or not n:
        return None, None
    take = min(k, n)
    recall = (take * g / n) / min(k, g)
    success = 1 - math.comb(n - g, take) / math.comb(n, take) if n - g >= take else 1.0
    return recall, success


def sign_test(better, worse):
    """Exact two-sided sign test p-value (ties left out)."""
    n = better + worse
    if not n:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(better, worse) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def cluster_bootstrap(deltas, clusters, b=10000, seed=20261027, alpha=0.05):
    """Mean of deltas (one per question) with a percentile CI from resampling clusters.
    Returns (mean, low, high)."""
    if not deltas:
        return None, None, None
    groups = {}
    for d, c in zip(deltas, clusters):
        groups.setdefault(c, []).append(d)
    members = list(groups.values())
    rng = random.Random(seed)
    means = []
    for _ in range(b):
        picked = [members[rng.randrange(len(members))] for _ in members]
        total = sum(sum(m) for m in picked)
        means.append(total / sum(len(m) for m in picked))
    means.sort()
    lo, hi = means[int(alpha / 2 * b)], means[min(b - 1, int((1 - alpha / 2) * b))]
    return sum(deltas) / len(deltas), lo, hi


def paired(deltas, clusters, b=10000, seed=20261027):
    """One comparison: per-cluster better/worse/tied (cluster mean delta), sign test and CI."""
    by = {}
    for d, c in zip(deltas, clusters):
        by.setdefault(c, []).append(d)
    cl = [sum(v) / len(v) for v in by.values()]
    better = sum(x > 1e-12 for x in cl)
    worse = sum(x < -1e-12 for x in cl)
    mean, lo, hi = cluster_bootstrap(deltas, clusters, b, seed)
    return dict(mean=mean, ci=[lo, hi], clusters=len(cl), better=better, worse=worse,
                tied=len(cl) - better - worse, p=sign_test(better, worse), questions=len(deltas))


def shapley(value, players):
    """Shapley value of each player; value(frozenset of players switched on) -> number.
    Values add up to value(all) - value(none)."""
    out = {p: 0.0 for p in players}
    orders = list(itertools.permutations(players))
    for order in orders:
        on = frozenset()
        for p in order:
            out[p] += value(on | {p}) - value(on)
            on = on | {p}
    return {p: v / len(orders) for p, v in out.items()}


def factorial(value, players):
    """Two-level factorial effects: main effects and interactions of every subset of
    players, each as a contrast over all cells (the usual 2^k definition)."""
    cells = [frozenset(c) for r in range(len(players) + 1) for c in itertools.combinations(players, r)]
    effects = {}
    for r in range(1, len(players) + 1):
        for term in itertools.combinations(players, r):
            total = sum((-1) ** sum(p not in cell for p in term) * value(cell) for cell in cells)
            effects[term] = total / 2 ** (len(players) - 1)
    return effects
