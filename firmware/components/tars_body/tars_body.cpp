#include "tars_body.h"

#ifdef USE_ESP32

#include "esphome/components/json/json_util.h"
#include "esphome/components/network/util.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include "esphome/core/version.h"

#include "monocypher-ed25519.h"

#include <cinttypes>
#include <cmath>

namespace esphome {
namespace tars_body {

static const char *const TAG = "tars_body";

static const uint32_t SAMPLE_RATE = 16000;  // the microphone's, and the brain's audio up
static const size_t BLOCK_SAMPLES = 1280;   // 80 ms: the brain's block, and the pre-roll's unit
static const size_t BLOCK_BYTES = BLOCK_SAMPLES * sizeof(int16_t);
// The pre-roll: what was heard before the wake, for the brain's double-check (the generic model's check_window_s,
// 3 s) with a margin. 45 blocks.
static const size_t PREROLL_SAMPLES = 45 * BLOCK_SAMPLES;
static const size_t UP_BYTES = 4 * SAMPLE_RATE * sizeof(int16_t);  // audio up waiting to be sent: 4 s
static const uint32_t PLAY_RATE = 24000;                            // the brain's voice
// A body holds at least 30 s of reply (the brain sends it faster than it plays); a long answer runs past that, so
// 64 s, in PSRAM. Fuller than that, the connection waits for room, which slows the brain's sending.
static const size_t REPLY_BYTES = 64 * PLAY_RATE * sizeof(int16_t);
static const float BODY_CEILING_S = 120.0f;  // the most a conversation streams, whatever the brain says
static const int PROTOCOL = 1;

void TarsBody::setup() {
  uint8_t seed[32], public_key[32];
  memcpy(seed, this->seed_, sizeof(seed));  // key_pair wipes the seed it's given
  crypto_ed25519_key_pair(this->secret_key_, public_key, seed);

  RAMAllocator<int16_t> allocator(RAMAllocator<int16_t>::ALLOC_EXTERNAL);
  this->preroll_ = allocator.allocate(PREROLL_SAMPLES);
  this->snapshot_ = allocator.allocate(PREROLL_SAMPLES);
  this->up_ = ring_buffer::RingBuffer::create(UP_BYTES);
  this->reply_ = ring_buffer::RingBuffer::create(REPLY_BYTES);
  if (this->preroll_ == nullptr || this->snapshot_ == nullptr || this->up_ == nullptr || this->reply_ == nullptr) {
    ESP_LOGE(TAG, "Not enough memory for the audio buffers");
    this->mark_failed();
    return;
  }

  this->mic_source_->add_data_callback([this](const std::vector<uint8_t> &data) { this->on_audio_(data); });
  this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 1, PLAY_RATE));

  esp_websocket_client_config_t config{};
  config.uri = this->url_.c_str();
  config.buffer_size = 4096;
  config.task_stack = 6144;
  config.reconnect_timeout_ms = 5000;
  config.network_timeout_ms = 10000;
  config.ping_interval_sec = 5;
  config.pingpong_timeout_sec = 15;  // a brain gone without closing (a power cut) is noticed in 15 s, not 2 min
  this->client_ = esp_websocket_client_init(&config);
  if (this->client_ == nullptr) {
    this->mark_failed();
    return;
  }
  esp_websocket_register_events(this->client_, WEBSOCKET_EVENT_ANY, &TarsBody::on_event_, this);
}

void TarsBody::dump_config() {
  ESP_LOGCONFIG(TAG,
                "TARS body:\n"
                "  Brain: %s\n"
                "  Device: %s\n"
                "  Pre-roll: %u ms",
                this->url_.c_str(), this->device_.c_str(), (unsigned) (PREROLL_SAMPLES * 1000 / SAMPLE_RATE));
}

// The connection's task

void TarsBody::on_event_(void *arg, esp_event_base_t base, int32_t event_id, void *event_data) {
  auto *self = static_cast<TarsBody *>(arg);
  auto *data = static_cast<esp_websocket_event_data_t *>(event_data);
  switch (event_id) {
    // Nothing here logs: a log line written from this task can block on the USB console when nobody reads it.
    case WEBSOCKET_EVENT_CONNECTED:
      break;
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
    case WEBSOCKET_EVENT_ERROR:
      self->taking_reply_ = false;
      self->dropped_ = true;
      break;
    case WEBSOCKET_EVENT_DATA:
      if (data->op_code == 0x1 || (data->op_code == 0x0 && !self->partial_.empty())) {
        // Text: one JSON object, maybe in pieces
        if (data->payload_offset == 0)
          self->partial_.clear();
        self->partial_.append(data->data_ptr, data->data_len);
        if (data->payload_offset + data->data_len < data->payload_len)
          break;
        std::string text;
        text.swap(self->partial_);
        // A reply's audio follows its `play` at once: take it from here, before loop() has read the `play`.
        if (text.find("\"play\"") != std::string::npos)
          self->taking_reply_ = true;
        else if (text.find("\"stop\"") != std::string::npos)
          self->taking_reply_ = false;
        std::lock_guard<std::mutex> lock(self->inbox_lock_);
        self->inbox_.push_back(std::move(text));
      } else if (data->op_code == 0x2 || data->op_code == 0x0) {
        // Audio: the reply
        if (self->taking_reply_ && data->data_len > 0) {
          size_t written = self->reply_->write_without_replacement(data->data_ptr, data->data_len, pdMS_TO_TICKS(2000));
          if (written < (size_t) data->data_len)
            self->reply_overflow_ += data->data_len - written;
        }
      }
      break;
    default:
      break;
  }
}

// The microphone's task

void TarsBody::on_audio_(const std::vector<uint8_t> &data) {
  const int16_t *samples = reinterpret_cast<const int16_t *>(data.data());
  size_t count = data.size() / sizeof(int16_t);
  std::lock_guard<std::mutex> lock(this->mic_lock_);
  for (size_t i = 0; i < count; i++) {
    this->preroll_[this->preroll_at_] = samples[i];
    this->preroll_at_ = (this->preroll_at_ + 1) % PREROLL_SAMPLES;
  }
  this->preroll_filled_ = std::min(PREROLL_SAMPLES, this->preroll_filled_ + count);
  if (this->listening_)
    this->up_->write(data.data(), count * sizeof(int16_t));
}

// loop()

void TarsBody::loop() {
  if (this->client_ != nullptr && !esp_websocket_client_is_connected(this->client_) && network::is_connected()) {
    static bool started = false;
    if (!started) {
      esp_websocket_client_start(this->client_);
      started = true;
    }
  }

  if (this->dropped_.exchange(false))
    this->lost_();

  while (true) {
    std::string text;
    {
      std::lock_guard<std::mutex> lock(this->inbox_lock_);
      if (this->inbox_.empty())
        break;
      text = std::move(this->inbox_.front());
      this->inbox_.pop_front();
    }
    this->handle_(text);
  }

  // Up: whole blocks, until the conversation's cap
  uint8_t block[BLOCK_BYTES];
  while (this->listening_ && this->up_->available() >= BLOCK_BYTES) {
    this->up_->read(block, BLOCK_BYTES, 0);
    this->streamed_samples_ += BLOCK_SAMPLES;
    float cap = std::min(BODY_CEILING_S, this->max_listen_s_);
    if (this->streamed_samples_ > cap * SAMPLE_RATE + 1) {
      ESP_LOGI(TAG, "Streamed for %.0f s: the conversation's cap", cap);
      this->stop_streaming_();
      this->awake_ = false;
      this->send_text_("{\"type\":\"gone_quiet\"}");
      break;
    }
    this->send_audio_(block, BLOCK_BYTES);
  }

  // Down: the reply, or the line said when the brain can't be reached
  this->feed_speaker_();

  if (uint32_t lost = this->reply_overflow_.exchange(0))
    ESP_LOGW(TAG, "The reply came faster than it could be kept: %" PRIu32 " bytes dropped", lost);
}

void TarsBody::handle_(const std::string &text) {
  JsonDocument said = json::parse_json(text);
  std::string kind = said["type"] | "";
  if (kind != "challenge")
    ESP_LOGD(TAG, "From the brain: %s", text.c_str());

  if (kind == "challenge") {
    std::string nonce = said["nonce"] | "";
    uint8_t signature[64];
    crypto_ed25519_sign(signature, this->secret_key_, reinterpret_cast<const uint8_t *>(nonce.data()), nonce.size());
    std::string hello = json::build_json([&](JsonObject root) {
      root["type"] = "hello";
      root["protocol"] = PROTOCOL;
      root["device"] = this->device_;
      root["proof"] = format_hex(signature, sizeof(signature));
      root["kind"] = "atom";
      root["wake"] = "model";  // the brain double-checks what the wake word heard
      root["echo_cancel"] = true;
      JsonArray buttons = root["buttons"].to<JsonArray>();
      buttons.add("talk");
      root["version"] = ESPHOME_VERSION;
    });
    this->send_text_(hello);
  } else if (kind == "welcome") {
    this->welcomed_ = true;
    this->max_listen_s_ = said["max_listen_s"] | 120.0f;
    int rate = said["play_rate"] | 0;
    std::string room = said["room"] | "";
    ESP_LOGI(TAG, "The brain let this body in: room %s", room.c_str());
    if (rate != (int) PLAY_RATE)
      ESP_LOGW(TAG, "The brain's voice is %d Hz; this body plays %" PRIu32 " Hz, so replies won't sound right", rate,
               PLAY_RATE);
  } else if (kind == "listen") {
    // Only inside a conversation this body opened: never from sleep, whatever the brain says.
    bool on = (said["on"] | false) && this->awake_;
    if (on && !this->listening_) {
      std::lock_guard<std::mutex> lock(this->mic_lock_);
      this->up_->reset();
      this->listening_ = true;
    } else if (!on) {
      this->stop_streaming_();
    }
  } else if (kind == "play") {
    if (this->playing_ >= 0)
      this->stop_playing_(true);
    this->playing_ = said["id"] | 0;
    this->play_ended_ = false;
    this->taking_reply_ = true;
    this->speaker_->start();
  } else if (kind == "play_end") {
    if ((said["id"] | -1) == this->playing_)
      this->play_ended_ = true;
  } else if (kind == "stop") {
    this->stop_playing_(true);
  } else if (kind == "sleep") {
    this->stop_streaming_();
    this->awake_ = false;
  } else if (kind == "mute") {
    this->muted_ = said["muted"] | false;
    if (this->muted_) {
      this->stop_streaming_();
      this->awake_ = false;
      this->mww_->stop();  // muted: the wake word isn't even running
    } else {
      this->mww_->start();
    }
    ESP_LOGI(TAG, "%s", this->muted_ ? "Muted" : "Listening for the name again");
  }
}

void TarsBody::wake(bool button, float score) {
  if (this->awake_ || this->muted_)
    return;
  if (!this->welcomed_) {
    ESP_LOGW(TAG, "Called, but the brain can't be reached");
    this->say_unreachable_();
    return;
  }

  // The pre-roll: the newest whole blocks heard. From here, what's heard goes up after it, with nothing in between.
  size_t count = 0;
  {
    std::lock_guard<std::mutex> lock(this->mic_lock_);
    if (!button) {
      count = (this->preroll_filled_ / BLOCK_SAMPLES) * BLOCK_SAMPLES;
      size_t start = (this->preroll_at_ + PREROLL_SAMPLES - count) % PREROLL_SAMPLES;
      for (size_t i = 0; i < count; i++)
        this->snapshot_[i] = this->preroll_[(start + i) % PREROLL_SAMPLES];
    }
    this->up_->reset();
    this->listening_ = true;
  }
  this->awake_ = true;
  this->streamed_samples_ = count;

  unsigned preroll_ms = count * 1000 / SAMPLE_RATE;
  char wake[128];
  snprintf(wake, sizeof(wake), "{\"type\":\"wake\",\"how\":\"%s\",\"score\":%.3f,\"preroll_ms\":%u}",
           button ? "button" : "word", score, preroll_ms);
  ESP_LOGI(TAG, "Woke (%s, score %.2f): %u ms of pre-roll go up first", button ? "button" : "word", score, preroll_ms);
  this->send_text_(wake);
  if (count > 0)
    this->send_audio_(reinterpret_cast<const uint8_t *>(this->snapshot_), count * sizeof(int16_t));
}

void TarsBody::press() {
  if (this->playing_ >= 0) {
    // While TARS speaks, the button stops it: the brain decides what follows.
    this->send_text_("{\"type\":\"button\",\"name\":\"stop\"}");
    this->stop_playing_(true);
    return;
  }
  this->wake(true, 1.0f);
}

void TarsBody::stop_streaming_() {
  std::lock_guard<std::mutex> lock(this->mic_lock_);
  this->listening_ = false;
  this->up_->reset();
}

void TarsBody::stop_playing_(bool tell) {
  this->taking_reply_ = false;
  this->reply_->reset();
  this->pending_len_ = 0;
  this->saying_unreachable_ = false;
  this->speaker_->stop();
  if (this->playing_ >= 0 && tell) {
    char played[64];
    snprintf(played, sizeof(played), "{\"type\":\"played\",\"id\":%" PRId32 ",\"cut\":true}", this->playing_);
    this->send_text_(played);
  }
  this->playing_ = -1;
}

void TarsBody::feed_speaker_() {
  if (this->saying_unreachable_) {
    size_t left = this->unreachable_len_ - this->unreachable_at_;
    if (left > 0) {
      size_t took = this->speaker_->play(this->unreachable_ + this->unreachable_at_, std::min<size_t>(left, 4096), 0);
      this->unreachable_at_ += took;
    } else if (!this->speaker_->has_buffered_data()) {
      this->speaker_->finish();
      this->saying_unreachable_ = false;
    }
    return;
  }
  if (this->playing_ < 0)
    return;

  for (int i = 0; i < 4; i++) {
    if (this->pending_len_ == 0) {
      size_t got = this->reply_->read(this->pending_, sizeof(this->pending_) & ~1u, 0);
      this->pending_len_ = got & ~(size_t) 1;
      if (got & 1)  // never split a sample: an odd byte waits for its other half
        this->reply_->write(this->pending_ + got - 1, 1);
    }
    if (this->pending_len_ == 0)
      break;
    size_t took = this->speaker_->play(this->pending_, this->pending_len_, 0);
    if (took == 0)
      break;
    memmove(this->pending_, this->pending_ + took, this->pending_len_ - took);
    this->pending_len_ -= took;
  }

  // Played: the brain said the audio is all in, and the last of it has left the speaker.
  if (this->play_ended_ && this->pending_len_ == 0 && this->reply_->available() == 0 &&
      !this->speaker_->has_buffered_data()) {
    this->speaker_->finish();
    char played[64];
    snprintf(played, sizeof(played), "{\"type\":\"played\",\"id\":%" PRId32 ",\"cut\":false}", this->playing_);
    ESP_LOGD(TAG, "Played %" PRId32, this->playing_);
    this->send_text_(played);
    this->playing_ = -1;
    this->taking_reply_ = false;
  }
}

void TarsBody::say_unreachable_() {
  if (this->unreachable_ == nullptr || this->saying_unreachable_ || this->playing_ >= 0)
    return;
  this->unreachable_at_ = 0;
  this->saying_unreachable_ = true;
  this->speaker_->start();
}

void TarsBody::lost_() {
  if (this->welcomed_)
    ESP_LOGW(TAG, "Lost the brain; trying again");
  this->welcomed_ = false;
  this->awake_ = false;
  this->stop_streaming_();
  this->stop_playing_(false);
  std::lock_guard<std::mutex> lock(this->inbox_lock_);
  this->inbox_.clear();
}

void TarsBody::send_text_(const std::string &text) {
  if (this->client_ == nullptr || !esp_websocket_client_is_connected(this->client_))
    return;
  esp_websocket_client_send_text(this->client_, text.data(), text.size(), pdMS_TO_TICKS(1000));
}

bool TarsBody::send_audio_(const uint8_t *data, size_t len) {
  if (this->client_ == nullptr || !esp_websocket_client_is_connected(this->client_))
    return false;
  return esp_websocket_client_send_bin(this->client_, reinterpret_cast<const char *>(data), len,
                                       pdMS_TO_TICKS(1000)) >= 0;
}

}  // namespace tars_body
}  // namespace esphome

#endif
