#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DECK_MAX_AGENTS 16
#define DECK_MAX_LINE   192

typedef enum {
    DeckIdle,
    DeckRunning,
    DeckWaiting,
    DeckDone,
    DeckError,
    DeckOffline,
} DeckState;

typedef struct {
    char id[17];
    char label[49];
    DeckState state;
    char event[9];
    char reason[31];
    char detail[49];
    bool can_focus;
    uint32_t actions;
    char control[9];
    bool controls_seen;
    char session_id[129];
    char project[257];
    char review[513];
    uint32_t session_length;
    uint32_t project_length;
    uint32_t review_length;
} DeckRow;

typedef struct {
    char session[9];
    uint32_t revision;
    size_t count;
    DeckRow rows[DECK_MAX_AGENTS];
} DeckSnapshot;

typedef struct {
    DeckSnapshot pending;
    size_t received;
    bool active;
} DeckParser;

// A partial or invalid transaction never changes the displayed snapshot.
bool deck_parse_line(DeckParser* parser, char* line, DeckSnapshot* current);
