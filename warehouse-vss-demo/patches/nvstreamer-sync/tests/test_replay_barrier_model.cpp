// SPDX-License-Identifier: Apache-2.0
// Deterministic, dependency-free model tests; no NvStreamer code is executed.
#include "replay_barrier_model.hpp"

#include <cassert>
#include <map>
#include <string>

using Result = ReplayBarrierModel::Result;

static void initial_five_readers(ReplayBarrierModel& barrier) {
    // Two independent reader sessions for Camera_01.mp4; the filename is
    // intentionally absent from the EOS accounting key.
    for (const auto& [id, seq] :
         std::map<std::string, std::uint32_t>{{"cam01-a", 101},
                                               {"cam01-b", 102},
                                               {"cam00", 103},
                                               {"cam02", 104},
                                               {"cam03", 105}}) {
        assert(barrier.add(id, seq));
        assert(barrier.seek_succeeded(id, seq));
    }
}

static void duplicate_eos_does_not_replay_early() {
    ReplayBarrierModel barrier;
    initial_five_readers(barrier);
    assert(barrier.eos("cam01-a", 101) == Result::waiting);
    assert(barrier.eos("cam01-a", 101) == Result::ignored);
    assert(barrier.eos("cam01-b", 102) == Result::waiting);
    assert(barrier.eos("cam00", 103) == Result::waiting);
    assert(barrier.eos("cam02", 104) == Result::waiting);
    assert(!barrier.ready());
    assert(barrier.eos("cam03", 105) == Result::replay_ready);
}

static void removed_and_replaced_reader_cannot_contribute_stale_eos() {
    ReplayBarrierModel barrier;
    initial_five_readers(barrier);
    assert(barrier.remove("cam01-a") == Result::waiting);
    assert(barrier.add("cam01-c", 106));
    assert(barrier.seek_succeeded("cam01-c", 106));
    assert(barrier.eos("cam01-a", 101) == Result::ignored);
    assert(barrier.eos("cam01-b", 102) == Result::waiting);
    assert(barrier.eos("cam00", 103) == Result::waiting);
    assert(barrier.eos("cam02", 104) == Result::waiting);
    assert(barrier.eos("cam03", 105) == Result::waiting);
    assert(!barrier.ready());
    assert(barrier.eos("cam01-c", 106) == Result::replay_ready);
}

static void delayed_eos_from_prior_generation_is_rejected() {
    ReplayBarrierModel barrier;
    initial_five_readers(barrier);
    for (const auto& [id, seq] :
         std::map<std::string, std::uint32_t>{{"cam01-a", 101},
                                               {"cam01-b", 102},
                                               {"cam00", 103},
                                               {"cam02", 104}}) {
        assert(barrier.eos(id, seq) == Result::waiting);
    }
    assert(barrier.eos("cam03", 105) == Result::replay_ready);
    assert(barrier.begin_next_generation({{"cam01-a", 201},
                                          {"cam01-b", 202},
                                          {"cam00", 203},
                                          {"cam02", 204},
                                          {"cam03", 205}}));
    assert(barrier.generation() == 1);
    // The asynchronous seek has not completed yet. Prior-loop callbacks
    // must not count even when the RTSP session remains active.
    assert(barrier.eos("cam01-a", 101) == Result::ignored);
    assert(barrier.eos("cam01-a", 201) == Result::ignored);
    for (const auto& [id, seq] :
         std::map<std::string, std::uint32_t>{{"cam01-a", 201},
                                               {"cam01-b", 202},
                                               {"cam00", 203},
                                               {"cam02", 204},
                                               {"cam03", 205}}) {
        assert(barrier.seek_succeeded(id, seq));
    }
    assert(barrier.eos("cam01-a", 101) == Result::ignored);
    assert(barrier.eos("cam01-a", 201) == Result::waiting);
    assert(barrier.eos("cam01-b", 202) == Result::waiting);
    assert(barrier.eos("cam00", 203) == Result::waiting);
    assert(barrier.eos("cam02", 204) == Result::waiting);
    assert(barrier.eos("cam03", 205) == Result::replay_ready);
}

static void removal_releases_arrived_survivors() {
    ReplayBarrierModel barrier;
    initial_five_readers(barrier);
    assert(barrier.eos("cam01-b", 102) == Result::waiting);
    assert(barrier.eos("cam00", 103) == Result::waiting);
    assert(barrier.eos("cam02", 104) == Result::waiting);
    assert(barrier.eos("cam03", 105) == Result::waiting);
    assert(barrier.remove("cam01-a") == Result::replay_ready);
    assert(barrier.member_count() == 4);
}

static void seek_failure_blocks_replay_until_failed_reader_leaves() {
    ReplayBarrierModel barrier;
    initial_five_readers(barrier);
    assert(barrier.eos("cam01-a", 101) == Result::waiting);
    assert(barrier.eos("cam01-b", 102) == Result::waiting);
    assert(barrier.eos("cam00", 103) == Result::waiting);
    assert(barrier.eos("cam02", 104) == Result::waiting);
    assert(barrier.seek_failed("cam03", 105));
    assert(barrier.eos("cam03", 105) == Result::ignored);
    assert(!barrier.ready());
    assert(barrier.remove("cam03") == Result::replay_ready);
}

static void invalid_generation_transition_is_rejected_without_mutation() {
    ReplayBarrierModel barrier;
    initial_five_readers(barrier);
    assert(!barrier.begin_next_generation({{"cam01-a", 201}}));
    assert(barrier.generation() == 0);
    assert(barrier.eos("cam01-a", 101) == Result::waiting);
    assert(barrier.eos("cam01-b", 102) == Result::waiting);
    assert(barrier.eos("cam00", 103) == Result::waiting);
    assert(barrier.eos("cam02", 104) == Result::waiting);
    assert(barrier.eos("cam03", 105) == Result::replay_ready);
    // Reusing one source's old seek sequence would confuse old and new EOS.
    assert(!barrier.begin_next_generation({{"cam01-a", 101},
                                           {"cam01-b", 202},
                                           {"cam00", 203},
                                           {"cam02", 204},
                                           {"cam03", 205}}));
    assert(barrier.generation() == 0);
}

static void callback_validated_before_replay_cannot_arrive_after_replay() {
    ReplayBarrierModel barrier;
    initial_five_readers(barrier);
    assert(barrier.eos("cam01-a", 101) == Result::waiting);
    assert(barrier.eos("cam01-b", 102) == Result::waiting);
    assert(barrier.eos("cam00", 103) == Result::waiting);
    assert(barrier.eos("cam02", 104) == Result::waiting);
    assert(barrier.eos("cam03", 105) == Result::replay_ready);

    // Duplicate old-loop EOS starts validation while the previous generation
    // remains current. The replay then arms generation 1 and clears arrivals
    // before this callback gets the group lock to insert its arrival.
    auto old_callback = barrier.begin_eos_validation("cam01-a", 101);
    assert(old_callback.preliminarily_valid);
    assert(old_callback.observed_generation == 0);
    assert(barrier.begin_next_generation({{"cam01-a", 201},
                                          {"cam01-b", 202},
                                          {"cam00", 203},
                                          {"cam02", 204},
                                          {"cam03", 205}}));
    assert(barrier.complete_eos(old_callback) == Result::ignored);
    assert(barrier.generation() == 1);
    assert(!barrier.ready());
    for (const auto& [id, seq] :
         std::map<std::string, std::uint32_t>{{"cam01-a", 201},
                                               {"cam01-b", 202},
                                               {"cam00", 203},
                                               {"cam02", 204},
                                               {"cam03", 205}}) {
        assert(barrier.seek_succeeded(id, seq));
    }
    assert(barrier.eos("cam01-a", 201) == Result::waiting);
    assert(barrier.eos("cam01-b", 202) == Result::waiting);
    assert(barrier.eos("cam00", 203) == Result::waiting);
    assert(barrier.eos("cam02", 204) == Result::waiting);
    assert(barrier.eos("cam03", 205) == Result::replay_ready);
}

int main() {
    duplicate_eos_does_not_replay_early();
    removed_and_replaced_reader_cannot_contribute_stale_eos();
    delayed_eos_from_prior_generation_is_rejected();
    removal_releases_arrived_survivors();
    seek_failure_blocks_replay_until_failed_reader_leaves();
    invalid_generation_transition_is_rejected_without_mutation();
    callback_validated_before_replay_cannot_arrive_after_replay();
}
