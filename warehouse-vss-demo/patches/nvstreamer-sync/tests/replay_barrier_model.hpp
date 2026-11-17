// SPDX-License-Identifier: Apache-2.0
// Standalone behavioral model for a proposed NvStreamer replay barrier.
// This is not linked into the NvStreamer image.
#pragma once

#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <utility>

class ReplayBarrierModel {
public:
    enum class Result { ignored, waiting, replay_ready };

    struct EosTicket {
        std::string session_id;
        std::uint32_t message_seqnum;
        std::uint64_t observed_generation;
        bool preliminarily_valid;
    };

    bool add(const std::string& session_id, std::uint32_t expected_seqnum) {
        std::lock_guard<std::mutex> guard(lock_);
        if (session_id.empty() || expected_seqnum == 0 ||
            members_.find(session_id) != members_.end()) {
            return false;
        }
        members_.emplace(session_id, Member{expected_seqnum});
        replay_ready_ = false;
        return true;
    }

    bool seek_succeeded(const std::string& session_id, std::uint32_t seqnum) {
        std::lock_guard<std::mutex> guard(lock_);
        auto it = members_.find(session_id);
        if (it == members_.end() || it->second.expected_seqnum != seqnum ||
            it->second.seek_failed) {
            return false;
        }
        it->second.seek_succeeded = true;
        return true;
    }

    bool seek_failed(const std::string& session_id, std::uint32_t seqnum) {
        std::lock_guard<std::mutex> guard(lock_);
        auto it = members_.find(session_id);
        if (it == members_.end() || it->second.expected_seqnum != seqnum) {
            return false;
        }
        it->second.seek_failed = true;
        it->second.seek_succeeded = false;
        it->second.arrived = false;
        replay_ready_ = false;
        return true;
    }

    Result eos(const std::string& session_id, std::uint32_t message_seqnum) {
        return complete_eos(begin_eos_validation(session_id, message_seqnum));
    }

    // The split methods expose the scheduling race to a deterministic test.
    // A real bus callback can inspect a message before another thread arms
    // the next loop. Only complete_eos may modify the arrival set, and it
    // revalidates identity and generation under the same group lock.
    EosTicket begin_eos_validation(const std::string& session_id,
                                   std::uint32_t message_seqnum) const {
        std::lock_guard<std::mutex> guard(lock_);
        auto it = members_.find(session_id);
        bool valid = it != members_.end() &&
                     it->second.expected_seqnum == message_seqnum &&
                     it->second.seek_succeeded && !it->second.seek_failed;
        return {session_id, message_seqnum, generation_, valid};
    }

    Result complete_eos(const EosTicket& ticket) {
        std::lock_guard<std::mutex> guard(lock_);
        auto it = members_.find(ticket.session_id);
        if (!ticket.preliminarily_valid ||
            ticket.observed_generation != generation_ ||
            it == members_.end() ||
            it->second.expected_seqnum != ticket.message_seqnum ||
            !it->second.seek_succeeded || it->second.seek_failed ||
            it->second.arrived) {
            return Result::ignored;
        }
        it->second.arrived = true;
        return evaluate_locked();
    }

    Result remove(const std::string& session_id) {
        std::lock_guard<std::mutex> guard(lock_);
        if (members_.erase(session_id) == 0) {
            return Result::ignored;
        }
        if (members_.empty()) {
            replay_ready_ = false;
            return Result::waiting;
        }
        return evaluate_locked();
    }

    // Called under the same group lock as EOS after every active source has
    // arrived. Every new expected seqnum must be registered before any async
    // flushing seek is posted, so late EOS from the prior loop is rejected.
    bool begin_next_generation(
        const std::map<std::string, std::uint32_t>& next_seqnums) {
        std::lock_guard<std::mutex> guard(lock_);
        if (!replay_ready_ || next_seqnums.size() != members_.size()) {
            return false;
        }
        for (const auto& [session_id, member] : members_) {
            auto next = next_seqnums.find(session_id);
            if (next == next_seqnums.end() || next->second == 0 ||
                next->second == member.expected_seqnum) {
                return false;
            }
        }
        for (auto& [session_id, member] : members_) {
            member = Member{next_seqnums.at(session_id)};
        }
        ++generation_;
        replay_ready_ = false;
        return true;
    }

    bool ready() const {
        std::lock_guard<std::mutex> guard(lock_);
        return replay_ready_;
    }
    std::uint64_t generation() const {
        std::lock_guard<std::mutex> guard(lock_);
        return generation_;
    }
    std::size_t member_count() const {
        std::lock_guard<std::mutex> guard(lock_);
        return members_.size();
    }

private:
    struct Member {
        std::uint32_t expected_seqnum;
        bool seek_succeeded = false;
        bool seek_failed = false;
        bool arrived = false;
    };

    Result evaluate_locked() {
        bool any_arrived = false;
        for (const auto& [session_id, member] : members_) {
            (void)session_id;
            any_arrived |= member.arrived;
            if (!member.arrived || !member.seek_succeeded || member.seek_failed) {
                return Result::waiting;
            }
        }
        if (any_arrived && !members_.empty()) {
            replay_ready_ = true;
            return Result::replay_ready;
        }
        return Result::waiting;
    }

    mutable std::mutex lock_;
    std::map<std::string, Member> members_;
    std::uint64_t generation_ = 0;
    bool replay_ready_ = false;
};
