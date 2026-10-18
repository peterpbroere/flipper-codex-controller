#include <furi.h>
#include <gui/gui.h>
#include <notification/notification_messages.h>
#include <stdio.h>
#include <string.h>

#include "deck_protocol.h"
#include "deck_settings.h"
#include "deck_usb.h"
#include "deck_ble.h"

typedef enum {
    ScreenOverview,
    ScreenDetail,
    ScreenIdentity,
    ScreenActions,
    ScreenConfirm,
    ScreenSettings
} DeckScreen;

typedef struct {
    FuriMutex* mutex;
    FuriMessageQueue* events;
    ViewPort* viewport;
    Gui* gui;
    NotificationApp* notifications;
    DeckUsb* usb;
    DeckBle* ble;
    bool bluetooth;
    bool switch_transport;
    size_t settings_selected;
    DeckSnapshot current;
    DeckParser parser;
    size_t selected;
    size_t top;
    size_t info_offset;
    size_t info_total;
    size_t action_selected;
    char target[17];
    char target_control[9];
    char target_connection[9];
    size_t target_verb;
    bool action_pending;
    uint32_t action_sent;
    DeckScreen screen;
    bool vibration;
    bool connected;
    uint32_t last_update;
    uint32_t sequence;
    uint32_t alerts;
    uint32_t buzzes;
    char toast[24];
    uint32_t toast_until;
} AgentDeck;

static bool send_line(AgentDeck* app, const char* line) {
    return app->bluetooth ? deck_ble_send(app->ble, line) : deck_usb_send(app->usb, line);
}

static const char* action_names[] =
    {"Open on PC", "Stop turn", "Continue", "Approve once", "Reject request"};
static const char* action_verbs[] = {"focus", "stop", "continue", "approve", "reject"};

static size_t action_count(AgentDeck* app) {
    size_t count = 0;
    if(app->current.count && app->connected) {
        uint32_t mask = app->current.rows[app->selected].actions;
        for(size_t i = 0; i < 5; i++) {
            if(mask & (1U << i)) count++;
        }
    }
    return count;
}

static size_t action_at(AgentDeck* app, size_t selected) {
    uint32_t mask = app->current.rows[app->selected].actions;
    for(size_t i = 0; i < 5; i++) {
        if((mask & (1U << i)) && selected-- == 0) return i;
    }
    return 5;
}

static void fit(Canvas* canvas, const char* source, int x, int y, size_t width) {
    char text[64];
    snprintf(text, sizeof(text), "%s", source);
    size_t length = strlen(text);
    bool clipped = false;
    while(length && canvas_string_width(canvas, text) > width) {
        text[--length] = '\0';
        clipped = true;
    }
    if(clipped && length >= 3) memcpy(text + length - 3, "...", 3);
    canvas_draw_str(canvas, x, y, text);
}

// Wrap by actual pixel width so paths and UUIDs remain readable without clipping.
static size_t identity_lines(Canvas* canvas, const char* source, size_t offset, bool render) {
    size_t number = 0;
    while(*source) {
        char line[65] = "";
        size_t used = 0;
        while(source[used] && source[used] != '\n' && used < sizeof(line) - 1) {
            line[used] = source[used];
            line[used + 1] = '\0';
            if(canvas_string_width(canvas, line) > 126) {
                line[used] = '\0';
                break;
            }
            used++;
        }
        if(render && number >= offset && number < offset + 3)
            canvas_draw_str(canvas, 1, 23 + 11 * (number - offset), line);
        source += used;
        if(*source == '\n') source++;
        number++;
    }
    return number;
}

static void draw(Canvas* canvas, void* context) {
    AgentDeck* app = context;
    if(furi_mutex_acquire(app->mutex, 25) != FuriStatusOk) return;
    canvas_clear(canvas);
    canvas_set_font(canvas, FontSecondary);
    char text[40];
    if(app->screen == ScreenSettings) {
        canvas_draw_str(canvas, 1, 8, "Settings");
        canvas_draw_line(canvas, 0, 10, 127, 10);
        canvas_draw_box(canvas, 0, 16 + 15 * app->settings_selected, 128, 13);
        canvas_set_color(canvas, ColorWhite);
        if(app->settings_selected != 0) canvas_set_color(canvas, ColorBlack);
        canvas_draw_str(canvas, 3, 25, "Vibration");
        canvas_draw_str(canvas, 102, 25, app->vibration ? "On" : "Off");
        canvas_set_color(canvas, app->settings_selected == 1 ? ColorWhite : ColorBlack);
        canvas_draw_str(canvas, 3, 40, "Connection");
        canvas_draw_str(canvas, 90, 40, app->bluetooth ? "BLE" : "USB");
        canvas_set_color(canvas, ColorBlack);
        canvas_draw_line(canvas, 0, 53, 127, 53);
        canvas_draw_str(canvas, 1, 63, "OK:Toggle  Back:List");
    } else if(app->screen == ScreenActions && app->current.count) {
        DeckRow* row = &app->current.rows[app->selected];
        fit(canvas, row->label, 1, 8, 126);
        canvas_draw_line(canvas, 0, 10, 127, 10);
        size_t count = action_count(app);
        if(app->action_selected >= count) app->action_selected = 0;
        if(!count) {
            canvas_draw_str(canvas, 1, 25, "No controls available");
            canvas_draw_str(canvas, 1, 40, "Use a shared local CLI");
        }
        size_t top = app->action_selected > 2 ? app->action_selected - 2 : 0;
        for(size_t i = top; i < count && i < top + 3; i++) {
            int y = 22 + 13 * (i - top);
            if(i == app->action_selected) {
                canvas_draw_box(canvas, 0, y - 9, 128, 12);
                canvas_set_color(canvas, ColorWhite);
            }
            canvas_draw_str(canvas, 3, y, action_names[action_at(app, i)]);
            canvas_set_color(canvas, ColorBlack);
        }
        canvas_draw_line(canvas, 0, 53, 127, 53);
        canvas_draw_str(canvas, 1, 63, "OK:Select  Back:Details");
    } else if(app->screen == ScreenConfirm && app->current.count) {
        DeckRow* row = &app->current.rows[app->selected];
        fit(canvas, action_names[app->target_verb], 1, 8, 126);
        canvas_draw_line(canvas, 0, 10, 127, 10);
        char content[640];
        const char* explanation =
            app->target_verb == 1 ? "Interrupt the current turn?" :
            app->target_verb == 2 ? "Send: Continue with the next logical step." :
                                    row->review;
        snprintf(content, sizeof(content), "%s\n%s", row->label, explanation);
        app->info_total = identity_lines(canvas, content, 0, false);
        size_t maximum = app->info_total > 3 ? app->info_total - 3 : 0;
        if(app->info_offset > maximum) app->info_offset = maximum;
        identity_lines(canvas, content, app->info_offset, true);
        canvas_draw_line(canvas, 0, 53, 127, 53);
        canvas_draw_str(canvas, 1, 63, "OK:Confirm Back:Cancel");
    } else if(app->screen == ScreenIdentity && app->current.count) {
        DeckRow* row = &app->current.rows[app->selected];
        char content[512];
        snprintf(
            content,
            sizeof(content),
            "Name: %s\nProject:\n%s\nSession ID:\n%s",
            row->label,
            *row->project ? row->project : "Not reported yet",
            *row->session_id ? row->session_id : row->id);
        app->info_total = identity_lines(canvas, content, 0, false);
        size_t maximum = app->info_total > 3 ? app->info_total - 3 : 0;
        if(app->info_offset > maximum) app->info_offset = maximum;
        snprintf(
            text,
            sizeof(text),
            "Session info %u/%u",
            (unsigned)app->info_offset + 1,
            (unsigned)maximum + 1);
        canvas_draw_str(canvas, 1, 8, text);
        canvas_draw_line(canvas, 0, 10, 127, 10);
        identity_lines(canvas, content, app->info_offset, true);
        canvas_draw_line(canvas, 0, 53, 127, 53);
        canvas_draw_str(canvas, 1, 63, "Up/Down:Scroll Back:Details");
    } else if(app->screen == ScreenDetail && app->current.count) {
        DeckRow* row = &app->current.rows[app->selected];
        fit(canvas, row->label, 1, 8, 126);
        canvas_draw_line(canvas, 0, 10, 127, 10);
        fit(canvas, app->connected ? row->reason : "Disconnected - status stale", 1, 23, 126);
        char first[25], second[25];
        snprintf(first, sizeof(first), "%.24s", row->detail);
        snprintf(
            second, sizeof(second), "%.24s", strlen(row->detail) > 24 ? row->detail + 24 : "");
        fit(canvas, first, 1, 35, 126);
        fit(canvas, second, 1, 46, 126);
        canvas_draw_line(canvas, 0, 53, 127, 53);
        canvas_draw_str(canvas, 1, 63, "OK:Controls >:Info Back:List");
    } else {
        size_t waiting = 0;
        for(size_t i = 0; i < app->current.count; i++) {
            if(app->current.rows[i].state == DeckWaiting ||
               app->current.rows[i].state == DeckError)
                waiting++;
        }
        snprintf(
            text,
            sizeof(text),
            "%u/%u  !%u",
            app->current.count ? (unsigned)app->selected + 1 : 0,
            (unsigned)app->current.count,
            (unsigned)waiting);
        canvas_draw_str(canvas, 1, 8, text);
        canvas_draw_str(canvas, 78, 8, app->vibration ? "V+" : "V-");
        canvas_draw_str(canvas, 105, 8, app->connected ? (app->bluetooth ? "BLE" : "USB") : "OFF");
        canvas_draw_line(canvas, 0, 10, 127, 10);
        if(!app->current.count) {
            canvas_draw_str(
                canvas,
                1,
                27,
                app->connected ? "No agents yet" :
                                 (app->bluetooth ? (app->ble ? "Waiting for BLE bridge" : "BLE unavailable") :
                                  (app->usb ? "Waiting for bridge" : "USB busy - restart app")));
            canvas_draw_str(canvas, 1, 42, "Back:Settings");
        }
        const char* states[] = {"IDLE", "RUN", "WAIT", "DONE", "ERR", "OFF"};
        for(size_t slot = 0; slot < 3 && app->top + slot < app->current.count; slot++) {
            size_t index = app->top + slot;
            DeckRow* row = &app->current.rows[index];
            int y = 21 + slot * 13;
            bool selected = index == app->selected;
            if(selected) {
                canvas_draw_box(canvas, 0, y - 9, 124, 12);
                canvas_set_color(canvas, ColorWhite);
            }
            if(row->state == DeckWaiting || row->state == DeckError)
                canvas_draw_str(canvas, 2, y, "!");
            fit(canvas, row->label, 10, y, 79);
            canvas_draw_str(canvas, 96, y, app->connected ? states[row->state] : "OFF");
            canvas_set_color(canvas, ColorBlack);
        }
        if(app->current.count > 3) {
            canvas_draw_line(canvas, 126, 12, 126, 49);
            int height = 37 * 3 / app->current.count;
            int y = 12 + (37 - height) * app->top / (app->current.count - 3);
            canvas_draw_box(canvas, 125, y, 3, height);
        }
        canvas_draw_line(canvas, 0, 53, 127, 53);
        canvas_draw_str(canvas, 1, 63, "OK:Details  >:Next!");
    }
    if(app->toast_until && (int32_t)(app->toast_until - furi_get_tick()) > 0) {
        canvas_set_color(canvas, ColorWhite);
        canvas_draw_box(canvas, 0, 52, 128, 12);
        canvas_set_color(canvas, ColorBlack);
        fit(canvas, app->toast, 1, 62, 126);
    }
    furi_mutex_release(app->mutex);
}

static void input_callback(InputEvent* input, void* context) {
    AgentDeck* app = context;
    DeckMessage message = {.type = DeckMessageInput, .data.input = *input};
    furi_message_queue_put(app->events, &message, 0);
}

static void scroll(AgentDeck* app) {
    if(!app->current.count)
        app->selected = app->top = 0;
    else if(app->selected >= app->current.count)
        app->selected = app->current.count - 1;
    if(app->selected < app->top) app->top = app->selected;
    if(app->selected >= app->top + 3) app->top = app->selected - 2;
    if(app->current.count <= 3)
        app->top = 0;
    else if(app->top > app->current.count - 3)
        app->top = app->current.count - 3;
}

static void toast(AgentDeck* app, const char* text) {
    snprintf(app->toast, sizeof(app->toast), "%s", text);
    app->toast_until = furi_get_tick() + furi_ms_to_ticks(1800);
}

static void send_action(AgentDeck* app, size_t verb) {
    if(!app->connected || (app->bluetooth && !deck_ble_connected(app->ble)) ||
       !app->current.count || app->action_pending) {
        toast(app, app->action_pending ? "Action in progress" : "Bridge disconnected");
        return;
    }
    DeckRow* row = &app->current.rows[app->selected];
    if(!(row->actions & (1U << verb)) ||
       (verb &&
        (strcmp(row->id, app->target) != 0 || strcmp(row->control, app->target_control) != 0 ||
         strcmp(app->current.session, app->target_connection) != 0))) {
        toast(app, "Changed - review again");
        app->screen = ScreenDetail;
        return;
    }
    char line[120];
    snprintf(
        line,
        sizeof(line),
        "ACT\t%s\t%lu\t%s\t%s\t%s\n",
        app->current.session,
        (unsigned long)++app->sequence,
        row->id,
        action_verbs[verb],
        verb ? app->target_control : row->control);
    if(send_line(app, line)) {
        app->action_pending = true;
        app->action_sent = furi_get_tick();
        toast(app, "Sending...");
        app->screen = ScreenDetail;
    } else {
        toast(app, "Link busy - try again");
    }
}

static bool input(AgentDeck* app, InputEvent event) {
    if(event.type == InputTypeLong && event.key == InputKeyBack) return false;
    if(event.type != InputTypeShort && event.type != InputTypeRepeat) return true;
    if(event.type == InputTypeRepeat && event.key != InputKeyUp && event.key != InputKeyDown)
        return true;
    if(app->screen == ScreenSettings) {
        if(event.key == InputKeyBack || event.key == InputKeyLeft)
            app->screen = ScreenOverview;
        else if(event.key == InputKeyUp || event.key == InputKeyDown)
            app->settings_selected = 1 - app->settings_selected;
        else if(event.key == InputKeyOk && app->settings_selected == 1) {
            if(deck_settings_save_bluetooth(!app->bluetooth))
                app->switch_transport = true;
            else
                toast(app, "Could not save setting");
        } else if(event.key == InputKeyOk) {
            if(deck_settings_save(!app->vibration)) {
                app->vibration = !app->vibration;
                toast(app, "Saved");
            } else
                toast(app, "Could not save setting");
        }
    } else if(app->screen == ScreenConfirm) {
        if(event.key == InputKeyBack || event.key == InputKeyLeft)
            app->screen = ScreenActions;
        else if(event.key == InputKeyUp && app->info_offset)
            app->info_offset--;
        else if(event.key == InputKeyDown && app->info_offset + 3 < app->info_total)
            app->info_offset++;
        else if(event.key == InputKeyOk)
            send_action(app, app->target_verb);
    } else if(app->screen == ScreenActions) {
        size_t count = action_count(app);
        if(event.key == InputKeyBack || event.key == InputKeyLeft)
            app->screen = ScreenDetail;
        else if(count) {
            if(app->action_selected >= count) app->action_selected = 0;
            if(event.key == InputKeyUp)
                app->action_selected = (app->action_selected + count - 1) % count;
            else if(event.key == InputKeyDown)
                app->action_selected = (app->action_selected + 1) % count;
            else if(event.key == InputKeyOk) {
                size_t verb = action_at(app, app->action_selected);
                if(verb == 0)
                    send_action(app, verb);
                else if(verb < 5) {
                    DeckRow* row = &app->current.rows[app->selected];
                    strcpy(app->target, row->id);
                    strcpy(app->target_control, row->control);
                    strcpy(app->target_connection, app->current.session);
                    app->target_verb = verb;
                    app->info_offset = 0;
                    app->screen = ScreenConfirm;
                }
            }
        }
    } else if(app->screen == ScreenIdentity) {
        if(event.key == InputKeyBack || event.key == InputKeyLeft)
            app->screen = ScreenDetail;
        else if(event.key == InputKeyUp && app->info_offset)
            app->info_offset--;
        else if(event.key == InputKeyDown && app->info_offset + 3 < app->info_total)
            app->info_offset++;
    } else if(app->screen == ScreenDetail) {
        if(event.key == InputKeyBack || event.key == InputKeyLeft)
            app->screen = ScreenOverview;
        else if(event.key == InputKeyRight) {
            app->screen = ScreenIdentity;
            app->info_offset = 0;
        } else if(event.key == InputKeyOk && app->current.count) {
            app->screen = ScreenActions;
            app->action_selected = 0;
        }
    } else {
        if(event.key == InputKeyBack)
            app->screen = ScreenSettings;
        else if(app->current.count) {
            if(event.key == InputKeyUp)
                app->selected = (app->selected + app->current.count - 1) % app->current.count;
            else if(event.key == InputKeyDown)
                app->selected = (app->selected + 1) % app->current.count;
            else if(event.key == InputKeyOk)
                app->screen = ScreenDetail;
            else if(event.key == InputKeyRight) {
                for(size_t i = 1; i <= app->current.count; i++) {
                    size_t next = (app->selected + i) % app->current.count;
                    if(app->current.rows[next].state == DeckWaiting ||
                       app->current.rows[next].state == DeckError) {
                        app->selected = next;
                        break;
                    }
                }
            }
        }
        scroll(app);
    }
    return true;
}

static void incoming_line(AgentDeck* app, char* line) {
    if(strcmp(line, "PING\tAD3") == 0) {
        send_line(app, "HELLO\tAD3\n");
        return;
    }
    if(strncmp(line, "RESULT\t", 7) == 0) {
        unsigned long sequence;
        char status[8], verb[12];
        if(sscanf(line + 7, "%lu\t%7s\t%11s", &sequence, status, verb) == 3) {
            if(!app->action_pending || sequence != app->sequence) return;
            toast(app, strcmp(status, "OK") == 0 ? "Action confirmed" : "Failed - check PC");
        } else {
            toast(app, strcmp(line + 7, "OK") == 0 ? "Terminal opened" : "Failed - check PC");
        }
        app->action_pending = false;
        return;
    }
    // Compare with the previous snapshot only at END, before atomic replacement.
    if(strncmp(line, "END\t", 4) == 0) {
        char selected[17] = "";
        if(app->current.count) strcpy(selected, app->current.rows[app->selected].id);
        bool new_alert = false;
        for(size_t i = 0; app->parser.active && i < app->parser.received; i++) {
            DeckRow* row = &app->parser.pending.rows[i];
            if(row->state != DeckWaiting || strcmp(row->event, "-") == 0) continue;
            bool seen = false;
            for(size_t j = 0; j < app->current.count; j++) {
                if(strcmp(row->id, app->current.rows[j].id) == 0 &&
                   strcmp(row->event, app->current.rows[j].event) == 0)
                    seen = true;
            }
            if(!seen) new_alert = true;
        }
        if(deck_parse_line(&app->parser, line, &app->current)) {
            app->last_update = furi_get_tick();
            app->connected = true;
            for(size_t i = 0; i < app->current.count; i++) {
                if(strcmp(app->current.rows[i].id, selected) == 0) app->selected = i;
            }
            scroll(app);
            if(!app->current.count &&
               (app->screen == ScreenDetail || app->screen == ScreenIdentity ||
                app->screen == ScreenActions || app->screen == ScreenConfirm))
                app->screen = ScreenOverview;
            if(app->screen == ScreenConfirm) {
                DeckRow* row = &app->current.rows[app->selected];
                if(strcmp(row->id, app->target) != 0 ||
                   strcmp(row->control, app->target_control) != 0 ||
                   strcmp(app->current.session, app->target_connection) != 0) {
                    app->screen = ScreenDetail;
                    toast(app, "Changed - review again");
                }
            }
            if(new_alert) {
                app->alerts++;
                if(app->vibration) {
                    app->buzzes++;
                    notification_message(app->notifications, &sequence_single_vibro);
                }
            }
            char ack[120];
            snprintf(
                ack,
                sizeof(ack),
                "ACK\t%s\t%lu\t%u\t%u\t%lu\t%lu\t%s\n",
                app->current.session,
                (unsigned long)app->current.revision,
                (unsigned)app->current.count,
                app->vibration,
                (unsigned long)app->alerts,
                (unsigned long)app->buzzes,
                app->current.count ? app->current.rows[app->selected].id : "-");
            send_line(app, ack);
        }
    } else {
        deck_parse_line(&app->parser, line, &app->current);
    }
}

int32_t agent_deck_app(void* context) {
    UNUSED(context);
    AgentDeck* app = malloc(sizeof(AgentDeck));
    memset(app, 0, sizeof(*app));
    app->vibration = deck_settings_load();
    app->bluetooth = deck_settings_load_bluetooth();
    app->mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->events = furi_message_queue_alloc(32, sizeof(DeckMessage));
    app->notifications = furi_record_open(RECORD_NOTIFICATION);
    app->gui = furi_record_open(RECORD_GUI);
    app->viewport = view_port_alloc();
    view_port_draw_callback_set(app->viewport, draw, app);
    view_port_input_callback_set(app->viewport, input_callback, app);
    gui_add_view_port(app->gui, app->viewport, GuiLayerFullscreen);
    if(app->bluetooth)
        app->ble = deck_ble_alloc(app->events);
    else
        app->usb = deck_usb_alloc(app->events);
    bool running = true;
    while(running) {
        DeckMessage message;
        bool received = furi_message_queue_get(app->events, &message, 100) == FuriStatusOk;
        furi_mutex_acquire(app->mutex, FuriWaitForever);
        if(received) {
            if(message.type == DeckMessageInput)
                running = input(app, message.data.input);
            else if(message.type == DeckMessageReset) {
                app->connected = app->action_pending = false;
                app->parser.active = false;
                app->current.session[0] = 0;
                if(app->screen == ScreenConfirm) app->screen = ScreenActions;
            } else
                incoming_line(app, message.data.line);
        }
        if(app->connected &&
           furi_get_tick() - app->last_update > furi_ms_to_ticks(app->bluetooth ? 10000 : 3000))
            app->connected = false;
        if(app->action_pending && furi_get_tick() - app->action_sent > furi_ms_to_ticks(12000)) {
            app->action_pending = false;
            toast(app, "Not confirmed - check PC");
        }
        furi_mutex_release(app->mutex);
        if(app->switch_transport) {
            deck_usb_free(app->usb);
            deck_ble_free(app->ble);
            app->usb = NULL;
            app->ble = NULL;
            // Old queued bytes must not cross a transport boundary.
            while(furi_message_queue_get(app->events, &message, 0) == FuriStatusOk) {
            }
            furi_mutex_acquire(app->mutex, FuriWaitForever);
            app->bluetooth = !app->bluetooth;
            app->connected = app->action_pending = false;
            app->parser.active = false;
            app->current.session[0] = 0;
            app->switch_transport = false;
            furi_mutex_release(app->mutex);
            if(app->bluetooth)
                app->ble = deck_ble_alloc(app->events);
            else
                app->usb = deck_usb_alloc(app->events);
            furi_mutex_acquire(app->mutex, FuriWaitForever);
            toast(app, (app->ble || app->usb) ? "Connection changed" : "Connection unavailable");
            furi_mutex_release(app->mutex);
        }
        view_port_update(app->viewport);
    }
    view_port_enabled_set(app->viewport, false);
    gui_remove_view_port(app->gui, app->viewport);
    deck_usb_free(app->usb);
    deck_ble_free(app->ble);
    view_port_free(app->viewport);
    furi_record_close(RECORD_GUI);
    furi_record_close(RECORD_NOTIFICATION);
    furi_message_queue_free(app->events);
    furi_mutex_free(app->mutex);
    free(app);
    return 0;
}
