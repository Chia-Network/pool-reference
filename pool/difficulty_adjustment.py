from typing import Tuple, List, Optional

from chia.util.ints import uint64


def get_new_difficulty(
    recent_partials: List[Tuple[uint64, uint64]],
    number_of_partials_target: int,
    time_target: int,
    current_difficulty: uint64,
    current_time: uint64,
    min_difficulty: uint64,
) -> uint64:
    """
    Given the last number_of_partials_target (or total partials we have) from a given farmer, return the new
    difficulty, or the same difficulty if we do not want to update.
    """

    # If we haven't processed any partials yet, maintain the current (default) difficulty
    if len(recent_partials) == 0:
        return current_difficulty

    # If we recently updated difficulty, don't update again
    if any(difficulty != current_difficulty for timestamp, difficulty in recent_partials):
        return current_difficulty

    # Lower the difficulty if we are really slow since our last partial
    last_timestamp = recent_partials[0][0]
    if current_time - last_timestamp > 3 * 3600:
        return max(min_difficulty, current_difficulty // 5)

    if current_time - last_timestamp > 3600:
        return max(min_difficulty, uint64(int(current_difficulty // 1.5)))

    # If we don't have enough partials at this difficulty, don't update yet
    if len(recent_partials) < number_of_partials_target:
        return current_difficulty

    # Finally, this is the standard case of normal farming and slow (or no) growth, adjust to the new difficulty
    time_taken = uint64(recent_partials[0][0] - recent_partials[-1][0])
    new_difficulty = uint64(int(current_difficulty * time_target / time_taken))
    return max(min_difficulty, new_difficulty)
