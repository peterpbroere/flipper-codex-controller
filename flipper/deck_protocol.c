#include "deck_protocol.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

static bool number(const char* text, uint32_t* output) {
    if(!*text || strlen(text) > 10) return false;
    uint64_t value = 0;
    for(const char* p = text; *p; p++) {
        if(*p < '0' || *p > '9') return false;
        value = value * 10 + (unsigned)(*p - '0');
        if(value > UINT32_MAX) return false;
    }
    *output = (uint32_t)value;
    return true;
}

static bool hex_token(const char* text) {
    if(strlen(text) != 8) return false;
    for(size_t i = 0; i < 8; i++) {
        if(!((text[i] >= '0' && text[i] <= '9') || (text[i] >= 'a' && text[i] <= 'f')))
            return false;
    }
    return true;
}

static bool identifier(const char* text) {
    size_t length = strlen(text);
    if(!length || length > 16) return false;
    for(size_t i = 0; i < length; i++) {
        if(!isalnum((unsigned char)text[i]) && text[i] != '_' && text[i] != '-') return false;
    }
    return true;
}

static bool copy_field(char* destination, size_t capacity, const char* text) {
    size_t length = strlen(text);
    if(!length || length >= capacity) return false;
    memcpy(destination, text, length + 1);
    return true;
}

bool deck_parse_line(DeckParser* parser, char* line, DeckSnapshot* current) {
    if(strlen(line) >= DECK_MAX_LINE) {
        parser->active = false;
        return false;
    }
    char* fields[9] = {line};
    size_t count = 1;
    for(char* p = line; *p; p++) {
        if(*p == '\t') {
            if(count == 9) {
                parser->active = false;
                return false;
            }
            *p = '\0';
            fields[count++] = p + 1;
        } else if((unsigned char)*p < 32 || (unsigned char)*p > 126) {
            parser->active = false;
            return false;
        }
    }
    if(strcmp(fields[0], "BEGIN") == 0) {
        uint32_t revision, expected;
        parser->active = false;
        if(count != 4 || !hex_token(fields[1]) || !number(fields[2], &revision) || !revision ||
           !number(fields[3], &expected) || expected > DECK_MAX_AGENTS)
            return false;
        memset(&parser->pending, 0, sizeof(parser->pending));
        strcpy(parser->pending.session, fields[1]);
        parser->pending.revision = revision;
        parser->pending.count = expected;
        parser->received = 0;
        parser->active = true;
        return false;
    }
    if(!parser->active) return false;
    if(strcmp(fields[0], "META") == 0) {
        uint32_t offset, total;
        if(count != 6 || !number(fields[3], &offset) || !number(fields[4], &total) || !*fields[5])
            goto invalid;
        DeckRow* row = NULL;
        for(size_t i = 0; i < parser->received; i++) {
            if(strcmp(parser->pending.rows[i].id, fields[1]) == 0) {
                row = &parser->pending.rows[i];
                break;
            }
        }
        if(!row) goto invalid;
        char* destination;
        size_t capacity;
        uint32_t* expected;
        if(strcmp(fields[2], "session_id") == 0) {
            destination = row->session_id;
            capacity = sizeof(row->session_id);
            expected = &row->session_length;
        } else if(strcmp(fields[2], "project") == 0) {
            destination = row->project;
            capacity = sizeof(row->project);
            expected = &row->project_length;
        } else if(strcmp(fields[2], "review") == 0) {
            destination = row->review;
            capacity = sizeof(row->review);
            expected = &row->review_length;
        } else
            goto invalid;
        size_t length = strlen(fields[5]);
        if(!total || total >= capacity || offset != strlen(destination) || length > total ||
           offset > total - length)
            goto invalid;
        if(!offset) *expected = total;
        if(*expected != total) goto invalid;
        memcpy(destination + offset, fields[5], length + 1);
        return false;
    }
    if(strcmp(fields[0], "CTRL") == 0) {
        uint32_t actions;
        if(count != 4 || !number(fields[2], &actions) || actions > 31 ||
           (!hex_token(fields[3]) && strcmp(fields[3], "-") != 0) ||
           ((actions & 30) && !hex_token(fields[3])))
            goto invalid;
        DeckRow* row = NULL;
        for(size_t i = 0; i < parser->received; i++) {
            if(strcmp(parser->pending.rows[i].id, fields[1]) == 0) {
                row = &parser->pending.rows[i];
                break;
            }
        }
        if(!row || row->controls_seen || ((actions & 1) && !row->can_focus)) goto invalid;
        row->actions = actions;
        strcpy(row->control, fields[3]);
        row->controls_seen = true;
        return false;
    }
    if(strcmp(fields[0], "ROW") == 0) {
        if(count != 8 || parser->received >= parser->pending.count || !identifier(fields[1]) ||
           (!hex_token(fields[4]) && strcmp(fields[4], "-") != 0) ||
           (strcmp(fields[7], "0") != 0 && strcmp(fields[7], "1") != 0))
            goto invalid;
        for(size_t i = 0; i < parser->received; i++) {
            if(strcmp(parser->pending.rows[i].id, fields[1]) == 0) goto invalid;
        }
        DeckRow* row = &parser->pending.rows[parser->received];
        const char* states[] = {"idle", "running", "waiting", "done", "error", "offline"};
        bool state_found = false;
        for(size_t i = 0; i < sizeof(states) / sizeof(states[0]); i++) {
            if(strcmp(states[i], fields[3]) == 0) {
                row->state = (DeckState)i;
                state_found = true;
                break;
            }
        }
        if(!state_found || !copy_field(row->id, sizeof(row->id), fields[1]) ||
           !copy_field(row->label, sizeof(row->label), fields[2]) ||
           !copy_field(row->event, sizeof(row->event), fields[4]) ||
           !copy_field(row->reason, sizeof(row->reason), fields[5]) ||
           !copy_field(row->detail, sizeof(row->detail), fields[6]))
            goto invalid;
        row->can_focus = strcmp(fields[7], "1") == 0;
        parser->received++;
        return false;
    }
    if(strcmp(fields[0], "END") == 0) {
        uint32_t revision;
        if(count != 2 || !number(fields[1], &revision) || revision != parser->pending.revision ||
           parser->received != parser->pending.count)
            goto invalid;
        for(size_t i = 0; i < parser->received; i++) {
            DeckRow* row = &parser->pending.rows[i];
            if(strlen(row->session_id) != row->session_length ||
               strlen(row->project) != row->project_length ||
               strlen(row->review) != row->review_length || !row->controls_seen)
                goto invalid;
        }
        parser->active = false;
        if(strcmp(current->session, parser->pending.session) == 0 && revision <= current->revision)
            return false;
        memcpy(current, &parser->pending, sizeof(*current));
        return true;
    }
invalid:
    parser->active = false;
    return false;
}
