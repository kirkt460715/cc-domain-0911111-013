import hashlib
from decimal import Decimal
from ..validation import canonical, reject

CONFIDENCE_Z = {
    "0.90": "1.6448536269514722",
    "0.95": "1.959963984540054",
    "0.99": "2.5758293035489004",
}


def mean(values):
    return sum(values, Decimal(0)) / len(values)


def sample_variance(values):
    if len(values) < 2:
        return Decimal(0)
    center = mean(values)
    return sum((value - center) ** 2 for value in values) / (len(values) - 1)


def stratified_effect(strata, kind):
    """Enrollment-weighted treatment effect across contributing strata.

    Each stratum is {"weight": Decimal, "treated": [Decimal], "control": [Decimal]}.
    Returns (estimate, variance, total_weight) where the first two are
    renormalized by the total contributing weight.
    """
    total_weight = sum(item["weight"] for item in strata)
    if total_weight <= 0:
        reject("analysis", "no contributing stratum weight")
    estimate = Decimal(0)
    variance = Decimal(0)
    for item in strata:
        treated, control = item["treated"], item["control"]
        treated_mean = mean(treated)
        control_mean = mean(control)
        estimate += item["weight"] * (treated_mean - control_mean)
        if kind == "binary":
            arm_variance = treated_mean * (1 - treated_mean) / len(
                treated
            ) + control_mean * (1 - control_mean) / len(control)
        else:
            arm_variance = sample_variance(treated) / len(treated) + sample_variance(
                control
            ) / len(control)
        variance += item["weight"] ** 2 * arm_variance
    return estimate / total_weight, variance / total_weight**2, total_weight


def interval(estimate, variance, confidence):
    z = Decimal(CONFIDENCE_Z[confidence])
    standard_error = variance.sqrt()
    return standard_error, estimate - z * standard_error, estimate + z * standard_error


class SplitMix64:
    """Self-contained deterministic generator; stable across runtimes."""

    MASK = (1 << 64) - 1

    def __init__(self, seed):
        self.state = seed & self.MASK

    def next(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & self.MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & self.MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & self.MASK
        return (z ^ (z >> 31)) & self.MASK

    def below(self, bound):
        if bound <= 1:
            return 0
        bits = (bound - 1).bit_length()
        while True:
            value = self.next() >> (64 - bits)
            if value < bound:
                return value


def seed_value(*parts):
    material = canonical([str(part) for part in parts]).encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def permutation_test(strata, permutations, seed):
    """Stratified randomization test preserving strata and units.

    strata: list of (values, treated_count, weight) with each value list
    ordered treated first; arm labels are permuted within each stratum so
    stratum membership and per-stratum arm sizes stay fixed. Returns
    (observed, exceedances, p_value) with a Monte Carlo corrected p-value.
    """
    prepared = []
    observed = 0.0
    for values, treated_count, weight in strata:
        total = sum(values)
        treated = sum(values[:treated_count])
        observed += weight * (
            treated / treated_count - (total - treated) / (len(values) - treated_count)
        )
        prepared.append((values, treated_count, weight, list(range(len(values))), total))
    rng = SplitMix64(seed)
    target = abs(observed)
    tolerance = 1e-9 * max(1.0, target)
    exceedances = 0
    for _ in range(permutations):
        statistic = 0.0
        for values, treated_count, weight, index, total in prepared:
            size = len(values)
            for i in range(treated_count):
                j = i + rng.below(size - i)
                index[i], index[j] = index[j], index[i]
            treated = 0.0
            for i in range(treated_count):
                treated += values[index[i]]
            statistic += weight * (
                treated / treated_count - (total - treated) / (size - treated_count)
            )
        if abs(statistic) >= target - tolerance:
            exceedances += 1
    p_value = Decimal(exceedances + 1) / Decimal(permutations + 1)
    return observed, exceedances, p_value


def ols_residuals(covariates, outcomes):
    """Ordinary least squares residuals with intercept, decimal arithmetic.

    covariates: list of Decimal rows without intercept; outcomes: Decimal list.
    """
    count = len(outcomes)
    width = len(covariates[0]) + 1
    if count <= width:
        reject("adjustment", "insufficient complete cases for covariate fit")
    matrix = [[Decimal(0)] * width for _ in range(width)]
    vector = [Decimal(0)] * width
    for row, outcome in zip(covariates, outcomes):
        xs = [Decimal(1)] + row
        for i in range(width):
            vector[i] += xs[i] * outcome
            for j in range(i, width):
                matrix[i][j] += xs[i] * xs[j]
    for i in range(width):
        for j in range(i):
            matrix[i][j] = matrix[j][i]
    for column in range(width):
        pivot = max(range(column, width), key=lambda r: abs(matrix[r][column]))
        if matrix[pivot][column] == 0:
            reject("adjustment", "covariate matrix is singular")
        if pivot != column:
            matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
            vector[column], vector[pivot] = vector[pivot], vector[column]
        for r in range(column + 1, width):
            factor = matrix[r][column] / matrix[column][column]
            if factor == 0:
                continue
            vector[r] -= factor * vector[column]
            for c in range(column, width):
                matrix[r][c] -= factor * matrix[column][c]
    beta = [Decimal(0)] * width
    for column in range(width - 1, -1, -1):
        remaining = vector[column] - sum(
            matrix[column][c] * beta[c] for c in range(column + 1, width)
        )
        beta[column] = remaining / matrix[column][column]
    residuals = []
    for row, outcome in zip(covariates, outcomes):
        xs = [Decimal(1)] + row
        fitted = sum(coefficient * value for coefficient, value in zip(beta, xs))
        residuals.append(outcome - fitted)
    return residuals


def holm_adjust(p_values):
    """Holm step-down adjusted p-values for one family, input order kept."""
    count = len(p_values)
    order = sorted(range(count), key=lambda i: (p_values[i], i))
    adjusted = [None] * count
    running = Decimal(0)
    for rank, index in enumerate(order):
        value = min((count - rank) * p_values[index], Decimal(1))
        running = max(running, value)
        adjusted[index] = running
    return adjusted
