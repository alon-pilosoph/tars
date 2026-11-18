#pragma once

#ifdef USE_ESP32

#include "esphome/components/micro_wake_word/micro_wake_word.h"
#include "esphome/components/microphone/microphone_source.h"
#include "esphome/components/ring_buffer/ring_buffer.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/core/defines.h"

#include <esp_websocket_client.h>

#include <atomic>
#include <deque>
#include <memory>
#include <mutex>
#include <string>

namespace esphome {
namespace tars_body {

// The body's half of TARS's body link (docs/bodies.md), as voice_assistant.bodyclient.BodyProtocol keeps it. The
// connection's task only takes in what the brain sends (texts queued in order, a reply's audio straight into the
// playback buffer); everything else, sending included, happens in loop(). The microphone's task keeps the pre-roll
// and, while streaming, fills the upload buffer.
class TarsBody : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void set_url(const std::string &url) { this->url_ = url; }
  void set_device(const std::string &device) { this->device_ = device; }
  void set_seed(const uint8_t *seed) { this->seed_ = seed; }
  void set_wake_word(micro_wake_word::MicroWakeWord *mww) { this->mww_ = mww; }
  void set_microphone_source(microphone::MicrophoneSource *source) { this->mic_source_ = source; }
  void set_speaker(speaker::Speaker *speaker) { this->speaker_ = speaker; }
  void set_unreachable(const uint8_t *pcm, size_t len) {
    this->unreachable_ = pcm;
    this->unreachable_len_ = len;
  }

  /// Someone called TARS: by name (`button` false, with the model's `score`) or with the button.
  void wake(bool button, float score);
  /// The button: stops TARS while it speaks, else starts a conversation.
  void press();

 protected:
  static void on_event_(void *arg, esp_event_base_t base, int32_t event_id, void *event_data);
  void on_audio_(const std::vector<uint8_t> &data);
  void handle_(const std::string &text);
  void send_text_(const std::string &text);
  bool send_audio_(const uint8_t *data, size_t len);
  void stop_streaming_();
  void stop_playing_(bool tell);
  void feed_speaker_();
  void lost_();
  void say_unreachable_();

  std::string url_;
  std::string device_;
  const uint8_t *seed_{nullptr};
  uint8_t secret_key_[64];
  micro_wake_word::MicroWakeWord *mww_{nullptr};
  microphone::MicrophoneSource *mic_source_{nullptr};
  speaker::Speaker *speaker_{nullptr};
  const uint8_t *unreachable_{nullptr};
  size_t unreachable_len_{0};
  size_t unreachable_at_{0};
  bool saying_unreachable_{false};

  esp_websocket_client_handle_t client_{nullptr};

  // From the connection's task to loop()
  std::mutex inbox_lock_;
  std::deque<std::string> inbox_;
  std::string partial_;
  std::atomic<bool> dropped_{false};
  std::atomic<bool> taking_reply_{false};  // a playback has started: its audio goes to the buffer
  std::atomic<uint32_t> reply_overflow_{0};

  // The pre-roll and the stream up, shared with the microphone's task
  std::mutex mic_lock_;
  int16_t *preroll_{nullptr};
  int16_t *snapshot_{nullptr};  // the pre-roll as it was at a wake, in PSRAM: too big for internal memory
  size_t preroll_at_{0};
  size_t preroll_filled_{0};
  std::atomic<bool> listening_{false};  // streaming: awake, and the brain hasn't said `listen off`
  std::unique_ptr<ring_buffer::RingBuffer> up_;

  // The conversation, loop() only
  bool welcomed_{false};
  bool awake_{false};
  bool muted_{false};
  float max_listen_s_{120.0f};
  uint32_t streamed_samples_{0};

  // The reply
  std::unique_ptr<ring_buffer::RingBuffer> reply_;
  int32_t playing_{-1};
  bool play_ended_{false};
  uint8_t pending_[1024];
  size_t pending_len_{0};
  size_t lead_in_left_{0};
  std::atomic<uint32_t> quiet_until_{0};  // what the microphone hears before this (millis) isn't sent up
#ifdef TARS_BODY_DEBUG_LEVELS
  double debug_energy_{0};
  size_t debug_samples_{0};
  const uint8_t *unreachable_save_{nullptr};
  size_t unreachable_save_len_{0};
#endif  // silence still to play before a playback, while the amplifier comes up
};

template<typename... Ts> class WakeAction : public Action<Ts...>, public Parented<TarsBody> {
 public:
  void set_button(bool button) { this->button_ = button; }
  TEMPLATABLE_VALUE(float, score)
  void play(const Ts &...x) override { this->parent_->wake(this->button_, this->score_.value(x...)); }

 protected:
  bool button_{false};
};

template<typename... Ts> class PressAction : public Action<Ts...>, public Parented<TarsBody> {
 public:
  void play(const Ts &...x) override { this->parent_->press(); }
};

}  // namespace tars_body
}  // namespace esphome

#endif
