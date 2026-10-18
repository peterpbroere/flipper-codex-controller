// Separate feasibility app: no agent-action commands are implemented here.
#include <furi.h>
#include <furi_hal_bt.h>
#include <gui/gui.h>
#include <bt/bt_service/bt.h>
#include <profiles/serial_profile.h>
#include <services/serial_service.h>
#include <services/battery_service.h>
#include <services/dev_info_service.h>
#include <storage/storage.h>
#include <stdio.h>
#include <string.h>

#include "../../flipper/deck_protocol.h"

typedef enum {
    EventInput,
    EventData,
    EventConnected,
    EventDisconnected
} EventType;
typedef struct {
    EventType type;
    InputEvent input;
} Event;

typedef struct {
    FuriHalBleProfileBase base;
    BleServiceSerial* serial;
    BleServiceBattery* battery;
    BleServiceDevInfo* info;
} Profile;

typedef struct {
    FuriMutex* mutex;
    FuriSemaphore* sent;
    FuriMessageQueue* events;
    FuriStreamBuffer* received;
    ViewPort* viewport;
    Bt* bt;
    Profile profile;
    DeckSnapshot current;
    DeckParser parser;
    char line[DECK_MAX_LINE];
    size_t used;
    bool dropping;
    size_t consumed_credit;
    volatile bool overflow;
    bool connected;
    bool ready;
    size_t selected;
    uint32_t updated;
    uint32_t sequence;
    uint32_t confirmed;
    uint32_t pending;
    uint32_t pending_at;
    char result[24];
} Probe;

static const FuriHalBleProfileTemplate profile_template;

static uint16_t serial_event(SerialServiceEvent event, void* context) {
    Probe* app = context;
    if(event.event == SerialServiceEventTypeDataReceived) {
        if(furi_stream_buffer_send(app->received, event.data.buffer, event.data.size, 0) !=
           event.data.size)
            app->overflow = true;
        Event message = {.type = EventData};
        // Wakeups coalesce; bytes live in a complete flow-control window.
        furi_message_queue_put(app->events, &message, 0);
    } else if(event.event == SerialServiceEventTypeDataSent) {
        furi_semaphore_release(app->sent);
    }
    return BLE_SVC_SERIAL_DATA_LEN_MAX;
}

static FuriHalBleProfileBase* profile_start(FuriHalBleProfileParams params) {
    Probe* app = params;
    Profile* profile = &app->profile;
    profile->base.config = &profile_template;
    profile->info = ble_svc_dev_info_start();
    profile->battery = ble_svc_battery_start(true);
    profile->serial = ble_svc_serial_start();
    furi_check(profile->serial);
    ble_svc_serial_set_callbacks(profile->serial, BLE_SVC_SERIAL_DATA_LEN_MAX, serial_event, app);
    return &profile->base;
}

static void profile_stop(FuriHalBleProfileBase* base) {
    Profile* profile = (Profile*)base;
    furi_check(base->config == &profile_template);
    ble_svc_serial_set_callbacks(profile->serial, 0, NULL, NULL);
    ble_svc_serial_stop(profile->serial);
    profile->serial = NULL;
    ble_svc_battery_stop(profile->battery);
    ble_svc_dev_info_stop(profile->info);
}

static void profile_config(GapConfig* config, FuriHalBleProfileParams params) {
    UNUSED(params);
    // Preserve official authenticated PIN pairing and connection parameters.
    ble_profile_serial->get_gap_config(config, NULL);
    config->mac_address[2] ^= 0xD3;
    snprintf(
        config->adv_name + 1,
        sizeof(config->adv_name) - 1,
        "Deck %s",
        furi_hal_version_get_name_ptr());
}

static const FuriHalBleProfileTemplate profile_template = {
    .start = profile_start,
    .stop = profile_stop,
    .get_gap_config = profile_config,
};

static void status_changed(BtStatus status, void* context) {
    Probe* app = context;
    Event message = {.type = status == BtStatusConnected ? EventConnected : EventDisconnected};
    if(furi_message_queue_put(app->events, &message, 0) != FuriStatusOk) app->overflow = true;
}

static bool send_line(Probe* app, const char* line) {
    if(!app->connected) return false;
    // One outstanding indication, bounded below the minimum ATT payload size.
    for(size_t offset = 0; offset < strlen(line); offset += 20) {
        while(furi_semaphore_acquire(app->sent, 0) == FuriStatusOk) {
        }
        size_t count = MIN((size_t)20, strlen(line) - offset);
        if(!ble_svc_serial_update_tx(app->profile.serial, (uint8_t*)line + offset, count) ||
           furi_semaphore_acquire(app->sent, 1000) != FuriStatusOk)
            return false;
    }
    return true;
}

static void line_received(Probe* app) {
    if(strcmp(app->line, "PING\tAD3B") == 0) {
        send_line(app, "HELLO\tAD3B\n");
    } else if(strncmp(app->line, "TESTED\t", 7) == 0) {
        char session[9];
        unsigned long sequence;
        if(sscanf(app->line, "TESTED\t%8s\t%lu", session, &sequence) == 2 &&
           strcmp(session, app->current.session) == 0 && app->pending &&
           sequence == app->pending) {
            app->confirmed++;
            app->pending = 0;
            snprintf(app->result, sizeof(app->result), "PC received test");
        }
    } else if(deck_parse_line(&app->parser, app->line, &app->current)) {
        // Even an unexpected host cannot add agent controls to this test app.
        for(size_t i = 0; i < app->current.count; i++)
            app->current.rows[i].actions = 0;
        app->ready = true;
        app->updated = furi_get_tick();
        if(app->selected >= app->current.count) app->selected = 0;
        char response[64];
        snprintf(
            response,
            sizeof(response),
            "ACK\t%s\t%lu\t%u\n",
            app->current.session,
            (unsigned long)app->current.revision,
            (unsigned)app->current.count);
        send_line(app, response);
    }
}

static void receive(Probe* app) {
    uint8_t bytes[243];
    size_t size;
    while((size = furi_stream_buffer_receive(app->received, bytes, sizeof(bytes), 0))) {
        for(size_t i = 0; i < size; i++) {
            uint8_t byte = bytes[i];
            if(byte == '\n') {
                if(app->used && !app->dropping) {
                    app->line[app->used] = 0;
                    line_received(app);
                }
                app->used = 0;
                app->dropping = false;
            } else if(byte != '\r') {
                if(byte == 0 || app->used >= sizeof(app->line) - 1)
                    app->dropping = true;
                else if(!app->dropping)
                    app->line[app->used++] = byte;
            }
        }
        app->consumed_credit += size;
        // Replenish after consuming the whole window. An empty-buffer check alone
        // races a final RX callback arriving between that check and replenishment.
        if(app->consumed_credit == BLE_SVC_SERIAL_DATA_LEN_MAX) {
            app->consumed_credit = 0;
            ble_svc_serial_notify_buffer_is_empty(app->profile.serial);
        }
    }
}

static void draw(Canvas* canvas, void* context) {
    Probe* app = context;
    if(furi_mutex_acquire(app->mutex, 25) != FuriStatusOk) return;
    canvas_clear(canvas);
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str(canvas, 1, 8, "Agent Deck BLE Test");
    canvas_draw_line(canvas, 0, 10, 127, 10);
    if(!app->connected) {
        canvas_draw_str(canvas, 1, 24, "Pair with Deck <name>");
        canvas_draw_str(canvas, 1, 36, "Run bluetooth probe on PC");
        canvas_draw_str(canvas, 1, 48, "Agent controls disabled");
    } else if(!app->ready) {
        canvas_draw_str(canvas, 1, 26, "BLE connected - waiting");
        canvas_draw_str(canvas, 1, 42, "No fresh agent snapshot");
    } else {
        static const char* states[] = {"IDLE", "RUN", "WAIT", "DONE", "ERR", "OFF"};
        size_t top = app->selected > 2 ? app->selected - 2 : 0;
        for(size_t i = top; i < app->current.count && i < top + 3; i++) {
            int y = 23 + 12 * (i - top);
            char text[24];
            snprintf(
                text,
                sizeof(text),
                "%c%.14s",
                i == app->selected ? '>' : ' ',
                app->current.rows[i].label);
            canvas_draw_str(canvas, 1, y, text);
            canvas_draw_str(canvas, 100, y, states[app->current.rows[i].state]);
        }
        if(!app->current.count) canvas_draw_str(canvas, 1, 29, "No reported sessions");
    }
    canvas_draw_line(canvas, 0, 53, 127, 53);
    canvas_draw_str(canvas, 1, 63, *app->result ? app->result : "OK:Test  Back:Exit");
    furi_mutex_release(app->mutex);
}

static void input(InputEvent* input, void* context) {
    Probe* app = context;
    Event message = {.type = EventInput, .input = *input};
    furi_message_queue_put(app->events, &message, 0);
}

int32_t agent_deck_ble_test_app(void* context) {
    UNUSED(context);
    Probe* app = malloc(sizeof(Probe));
    memset(app, 0, sizeof(*app));
    app->mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->sent = furi_semaphore_alloc(1, 0);
    app->events = furi_message_queue_alloc(8, sizeof(Event));
    app->received = furi_stream_buffer_alloc(BLE_SVC_SERIAL_DATA_LEN_MAX, 1);
    app->bt = furi_record_open(RECORD_BT);
    Storage* storage = furi_record_open(RECORD_STORAGE);
    storage_simply_mkdir(storage, APP_DATA_PATH(""));
    furi_record_close(RECORD_STORAGE);
    bt_disconnect(app->bt);
    bt_keys_storage_set_storage_path(app->bt, APP_DATA_PATH("ble.keys"));
    bt_set_status_changed_callback(app->bt, status_changed, app);
    bool started = bt_profile_start(app->bt, &profile_template, app) != NULL;
    Gui* gui = furi_record_open(RECORD_GUI);
    app->viewport = view_port_alloc();
    view_port_draw_callback_set(app->viewport, draw, app);
    view_port_input_callback_set(app->viewport, input, app);
    gui_add_view_port(gui, app->viewport, GuiLayerFullscreen);
    if(started) furi_hal_bt_start_advertising();
    bool running = started;
    while(running) {
        Event event;
        bool received = furi_message_queue_get(app->events, &event, 50) == FuriStatusOk;
        furi_mutex_acquire(app->mutex, FuriWaitForever);
        if(received && event.type == EventInput && event.input.key == InputKeyBack &&
           (event.input.type == InputTypeShort || event.input.type == InputTypeLong)) {
            running = false;
        } else if(received && event.type == EventInput && event.input.type == InputTypeShort) {
            if(event.input.key == InputKeyUp && app->selected) app->selected--;
            if(event.input.key == InputKeyDown && app->selected + 1 < app->current.count)
                app->selected++;
            if(event.input.key == InputKeyOk && app->connected && app->ready && !app->pending) {
                char line[80];
                app->pending = ++app->sequence;
                app->pending_at = furi_get_tick();
                snprintf(
                    line,
                    sizeof(line),
                    "TEST\t%s\t%lu\n",
                    app->current.session,
                    (unsigned long)app->pending);
                bool sent = send_line(app, line);
                if(!sent) app->pending = 0;
                snprintf(
                    app->result,
                    sizeof(app->result),
                    "%s",
                    sent ? "Test sent..." : "Test not sent");
            }
        } else if(received && (event.type == EventConnected || event.type == EventDisconnected)) {
            app->connected = event.type == EventConnected;
            app->ready = false;
            app->pending = app->used = 0;
            app->consumed_credit = 0;
            app->dropping = false;
            app->parser.active = false;
            *app->result = 0;
        }
        if(app->connected)
            receive(app);
        else {
            uint8_t discard[64];
            while(furi_stream_buffer_receive(app->received, discard, sizeof(discard), 0)) {
            }
        }
        if(app->ready && furi_get_tick() - app->updated > furi_ms_to_ticks(5000))
            app->ready = false;
        if(app->pending && furi_get_tick() - app->pending_at > furi_ms_to_ticks(5000)) {
            app->pending = 0;
            snprintf(app->result, sizeof(app->result), "Test timed out");
        }
        bool overflow = app->overflow;
        app->overflow = false;
        furi_mutex_release(app->mutex);
        if(overflow) bt_disconnect(app->bt);
        view_port_update(app->viewport);
    }
    // Stop GAP/service callbacks before releasing any application-owned state.
    bt_set_status_changed_callback(app->bt, NULL, NULL);
    bt_disconnect(app->bt);
    bt_keys_storage_set_default_path(app->bt);
    bt_profile_restore_default(app->bt);
    view_port_enabled_set(app->viewport, false);
    gui_remove_view_port(gui, app->viewport);
    view_port_free(app->viewport);
    furi_record_close(RECORD_GUI);
    furi_record_close(RECORD_BT);
    furi_message_queue_free(app->events);
    furi_stream_buffer_free(app->received);
    furi_semaphore_free(app->sent);
    furi_mutex_free(app->mutex);
    free(app);
    return 0;
}
