import random
from secrets import token_bytes

from chia.consensus.pos_quality import _expected_plot_size
from chia.consensus.pot_iterations import calculate_iterations_quality

# This is the sub slot iters that all pools must use to base the difficulty on. A difficulty of 1 (the minimum)
# targets about 10 proofs per day for one plot of size k32. There are 9216 signage points per day, 64 signage points
# per slot, the difficulty constant factor is 2**67, the plot filter is 512
ssi = (2**67 / (_expected_plot_size(32)) / 9216) * 64 * 10 * 512

print(f"SSi {ssi}")
# We use 37.6 billion as an approximation and a number that is easier to remember
ssi = 37600000000


def do_simulation(days: int, diff: int, k: int, num: int):
    successes = 0
    for i in range(int(9216 * days)):
        for j in range(num):
            # Plot filter
            if random.random() < (1.0 / 512.0):
                s = calculate_iterations_quality(2**67, token_bytes(32), k, diff, token_bytes(32)) < (ssi // 64)
                if s:
                    successes += 1
    return successes


# Difficulty 1, 3 minutes with 1 plot
print(do_simulation((1.0 / 480), 10, 22, 318))

# Difficulty 1, 1 day with 1 plot
print(do_simulation(1, 1, 32, 1))

# Difficulty 1, 400 days with 1 plot
print(do_simulation(400, 1, 32, 1))

# Difficulty 100, 10 day with 1 plot = 101GB
print(do_simulation(10, 100, 32, 1))

# Difficulty 100, 1 day with 100 plots = 10TB
print(do_simulation(1, 100, 32, 100))

# Difficulty 5000, 1 day with 10340 plots = 1PiB
print(do_simulation(1, 5000, 32, 10340))


# Test
# total_su = 0
# for _ in range(1000):
#     su = do_simulation(1, 1, 32, 1)
#     total_su += su
# print(total_su / 1000)
# assert abs(1 - (total_su / 1000) / 1) < 0.01
