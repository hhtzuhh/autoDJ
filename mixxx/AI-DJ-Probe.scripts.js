// State feedback for the AI DJ virtual MIDI probe.
// Input CCs are declared in AI-DJ-Probe.midi.xml. Output CCs are distinct so
// the CLI can distinguish commands from Mixxx's actual state.

var AiDjProbe = {};

AiDjProbe.connections = [];
AiDjProbe.lastMidiValues = {};
AiDjProbe.preciseSeekParts = {1: [0, 0, 0, 0, 0], 2: [0, 0, 0, 0, 0]};
AiDjProbe.preciseSeekMasks = {1: 0, 2: 0};
AiDjProbe.bassTargetDeck = 0;
AiDjProbe.tempoPacket = null;

AiDjProbe.clamp = function (value, low, high) {
    return Math.max(low, Math.min(high, value));
};

AiDjProbe.sendState = function (deckIndex, control, value, force) {
    var ccByControl = {
        play: 0x10,
        playposition: 0x11,
        rate: 0x12,
        volume: 0x13
    };
    var normalized = value;
    if (control === "rate") {
        normalized = (value + 1.0) / 2.0;
    }
    var midiValue = Math.round(AiDjProbe.clamp(normalized, 0.0, 1.0) * 127);
    var stateKey = deckIndex + ":" + control;
    if (!force && AiDjProbe.lastMidiValues[stateKey] === midiValue) {
        return;
    }
    AiDjProbe.lastMidiValues[stateKey] = midiValue;
    midi.sendShortMsg(0xB0 + deckIndex - 1, ccByControl[control], midiValue);
};

AiDjProbe.input = function (channel, control, value, status, group) {
    if (control === 0x1E && AiDjProbe.tempoPacket) {
        var packet = AiDjProbe.tempoPacket;
        if (Date.now() - packet.started < 250 && value >= 64 && value <= 126) {
            packet.parts.push(value - 64);
            if (packet.parts.length === 3) {
                var ratio = (packet.parts[0] + 63 * packet.parts[1] + 3969 * packet.parts[2]) / 100000;
                var deckGroup = "[Channel" + packet.deck + "]";
                var range = engine.getValue(deckGroup, "rateRange");
                var direction = engine.getValue(deckGroup, "rate_dir");
                // Mixxx 2.5 rate supports out-of-slider-range values. Preserve
                // user rateRange and rate_dir; do not edit track beat metadata.
                if (ratio >= 0.8 && ratio <= 1.2 && range > 0 && Math.abs(direction) === 1) {
                    engine.setValue(deckGroup, "rate", direction * (ratio - 1) / range);
                }
                AiDjProbe.tempoPacket = null;
            }
            return;
        }
        AiDjProbe.tempoPacket = null;
    }
    var normalized = value / 127.0;
    if (control === 0x14) {
        engine.setValue(group, "play", value > 0 ? 1 : 0);
    } else if (control === 0x15) {
        engine.setValue(group, "playposition", normalized);
    } else if (control === 0x16) {
        engine.setValue(group, "rate", value === 64 ? 0.0 : normalized * 2.0 - 1.0);
    } else if (control === 0x17) {
        engine.setValue(group, "volume", normalized);
    } else if (control === 0x18 && group === "[Master]") {
        engine.setValue(group, "crossfader", normalized * 2.0 - 1.0);
    } else if (control === 0x1D) {
        engine.setValue("[Playlist]", "SelectPrevTrack", 1);
    } else if (control === 0x1E && value === 127) {
        engine.setValue("[Playlist]", "SelectNextTrack", 1);
    } else if (control === 0x1E && value >= 1 && value <= 3) {
        AiDjProbe.recordingCommand(value);
    } else if (control === 0x1E && value >= 4 && value <= 5) {
        var statusDeckIndex = value - 3;
        AiDjProbe.sendDeckStatus(
            statusDeckIndex,
            "[Channel" + statusDeckIndex + "]"
        );
    } else if (control === 0x1E && value >= 6 && value <= 7) {
        AiDjProbe.sendObserverSnapshot(value - 5);
    } else if (control === 0x1E && value >= 10 && value <= 23) {
        AiDjProbe.syncCommand(value);
    } else if (control === 0x1E && value >= 24 && value <= 33) {
        engine.setValue("[Sampler" + (value - 23) + "]", "LoadSelectedTrack", 1);
    } else if (control === 0x1E && (value === 34 || value === 35)) {
        AiDjProbe.tempoPacket = {deck: value - 33, parts: [], started: Date.now()};
    } else if (control === 0x1E && value === 36) {
        midi.sendShortMsg(0xB0, 0x4B, 2); // Parameterized-routine protocol v2.
    } else if (control === 0x1E && value >= 40 && value <= 41) {
        AiDjProbe.bassCommand(value);
    } else if (control === 0x1E && value >= 42 && value <= 51) {
        engine.setValue("[Channel1]", "LoadTrackFromSampler", value - 41);
    } else if (control === 0x1E && value >= 52 && value <= 61) {
        engine.setValue("[Channel2]", "LoadTrackFromSampler", value - 51);
    } else if (control === 0x1E && value >= 64 && value <= 126) {
        AiDjProbe.bassValue(value);
    } else if (control === 0x1F) {
        engine.setValue("[Channel1]", "LoadSelectedTrack", 1);
    } else if (control === 0x20) {
        engine.setValue("[Channel2]", "LoadSelectedTrack", 1);
    } else if (control >= 0x21 && control <= 0x25) {
        AiDjProbe.receivePreciseSeekPart(channel + 1, control - 0x21, value);
    } else if (control === 0x26) {
        AiDjProbe.commitPreciseSeek(channel + 1, group, value === 2);
    }
};

AiDjProbe.bassCommand = function (value) {
    AiDjProbe.bassTargetDeck = value - 39;
};

AiDjProbe.bassValue = function (value) {
    if (AiDjProbe.bassTargetDeck === 0) {
        return;
    }
    var normalized = (value - 64) / 62.0;
    var group = "[Channel" + AiDjProbe.bassTargetDeck + "]";
    engine.setParameter(
        "[EqualizerRack1_" + group + "_Effect1]",
        "parameter1",
        normalized
    );
    AiDjProbe.bassTargetDeck = 0;
};

AiDjProbe.syncCommand = function (value) {
    var commands = {
        10: ["[Channel1]", "sync_leader", 1],
        11: ["[Channel1]", "sync_leader", 0],
        12: ["[Channel2]", "sync_leader", 1],
        13: ["[Channel2]", "sync_leader", 0],
        20: ["[Channel1]", "sync_enabled", 1],
        21: ["[Channel1]", "sync_enabled", 0],
        22: ["[Channel2]", "sync_enabled", 1],
        23: ["[Channel2]", "sync_enabled", 0]
    };
    var command = commands[value];
    if (command) {
        engine.setValue(command[0], command[1], command[2]);
    }
};

AiDjProbe.sendIntegerParts = function (deckIndex, firstControl, value, partCount) {
    var remaining = Math.max(0, Math.round(value));
    for (var partIndex = 0; partIndex < partCount; partIndex++) {
        midi.sendShortMsg(
            0xB0 + deckIndex - 1,
            firstControl + partIndex,
            remaining % 128
        );
        remaining = Math.floor(remaining / 128);
    }
};

AiDjProbe.sendDeckStatus = function (deckIndex, group) {
    midi.sendShortMsg(
        0xB0 + deckIndex - 1,
        0x33,
        Math.round(engine.getValue(group, "sync_mode"))
    );
    AiDjProbe.sendIntegerParts(
        deckIndex,
        0x34,
        engine.getValue(group, "bpm") * 1000,
        3
    );
    AiDjProbe.sendIntegerParts(
        deckIndex,
        0x37,
        engine.getValue(group, "file_bpm") * 1000,
        3
    );
    midi.sendShortMsg(0xB0 + deckIndex - 1, 0x3A, 1);
};

AiDjProbe.sendRecordingStatus = function (value) {
    midi.sendShortMsg(0xB0, 0x2E, Math.round(value));
};

AiDjProbe.recordingCommand = function (value) {
    var status = engine.getValue("[Recording]", "status");
    if ((value === 1 && status === 0) || (value === 2 && status !== 0)) {
        engine.setValue("[Recording]", "toggle_recording", 1);
    } else {
        AiDjProbe.sendRecordingStatus(status);
    }
};

AiDjProbe.receivePreciseSeekPart = function (deckIndex, partIndex, value) {
    AiDjProbe.preciseSeekParts[deckIndex][partIndex] = value;
    AiDjProbe.preciseSeekMasks[deckIndex] |= 1 << partIndex;
};

AiDjProbe.sendPreciseSeekResult = function (deckIndex, position, result) {
    var remaining = Math.max(0, Math.round(position));
    for (var partIndex = 0; partIndex < 5; partIndex++) {
        midi.sendShortMsg(
            0xB0 + deckIndex - 1,
            0x27 + partIndex,
            remaining % 128
        );
        remaining = Math.floor(remaining / 128);
    }
    midi.sendShortMsg(0xB0 + deckIndex - 1, 0x2C, result);
};

AiDjProbe.commitPreciseSeek = function (deckIndex, group, playAfterSeek) {
    if (AiDjProbe.preciseSeekMasks[deckIndex] !== 0x1F) {
        AiDjProbe.sendPreciseSeekResult(deckIndex, 0, 2);
        return;
    }

    var parts = AiDjProbe.preciseSeekParts[deckIndex];
    var requestedPosition = 0;
    for (var partIndex = 0; partIndex < parts.length; partIndex++) {
        requestedPosition += parts[partIndex] * Math.pow(128, partIndex);
    }
    AiDjProbe.preciseSeekMasks[deckIndex] = 0;

    var trackSamples = engine.getValue(group, "track_samples");
    if (trackSamples <= 0) {
        AiDjProbe.sendPreciseSeekResult(deckIndex, 0, 3);
        return;
    }
    if (requestedPosition < 0 || requestedPosition > trackSamples) {
        AiDjProbe.sendPreciseSeekResult(deckIndex, requestedPosition, 4);
        return;
    }

    engine.setValue(group, "play", 0);
    engine.setValue(group, "playposition", requestedPosition / trackSamples);
    if (playAfterSeek) {
        engine.setValue(group, "play", 1);
    }
    var actualPosition = Math.round(
        engine.getValue(group, "playposition") * trackSamples
    );
    AiDjProbe.sendPreciseSeekResult(deckIndex, actualPosition, 1);
};

AiDjProbe.sendTrackLoaded = function (deckIndex, value) {
    midi.sendShortMsg(0xB0 + deckIndex - 1, 0x1A, value > 0 ? 127 : 0);
};

AiDjProbe.sendTrackFingerprint = function (deckIndex, group) {
    var trackSamples = Math.round(engine.getValue(group, "track_samples"));
    var sampleRate = Math.round(engine.getValue(group, "track_samplerate"));
    var fileBpm = Math.round(engine.getValue(group, "file_bpm") * 1000);
    if (trackSamples <= 0 || sampleRate <= 0 || fileBpm <= 0) {
        return;
    }
    AiDjProbe.sendIntegerParts(deckIndex, 0x3C, trackSamples, 5);
    AiDjProbe.sendIntegerParts(deckIndex, 0x41, sampleRate, 3);
    AiDjProbe.sendIntegerParts(deckIndex, 0x44, fileBpm, 3);
    midi.sendShortMsg(0xB0 + deckIndex - 1, 0x47, 1);
};

AiDjProbe.sendLoopEnabled = function (deckIndex, value) {
    midi.sendShortMsg(0xB0 + deckIndex - 1, 0x3B, value > 0 ? 127 : 0);
};

AiDjProbe.sendSyncMode = function (deckIndex, value) {
    midi.sendShortMsg(0xB0 + deckIndex - 1, 0x48, Math.round(value));
};

AiDjProbe.sendCrossfader = function (value, force) {
    var midiValue = Math.round(AiDjProbe.clamp((value + 1.0) / 2.0, 0.0, 1.0) * 127);
    if (!force && AiDjProbe.lastMidiValues.crossfader === midiValue) {
        return;
    }
    AiDjProbe.lastMidiValues.crossfader = midiValue;
    midi.sendShortMsg(0xB0, 0x19, midiValue);
};

AiDjProbe.sendChannelShape = function (deckIndex, control, value, force) {
    var ccByControl = {eq_low: 0x49, filter: 0x4A};
    var midiValue = Math.round(AiDjProbe.clamp(value, 0.0, 1.0) * 127);
    var stateKey = deckIndex + ":" + control;
    if (!force && AiDjProbe.lastMidiValues[stateKey] === midiValue) {
        return;
    }
    AiDjProbe.lastMidiValues[stateKey] = midiValue;
    midi.sendShortMsg(0xB0 + deckIndex - 1, ccByControl[control], midiValue);
};

AiDjProbe.sendObserverSnapshot = function (deckIndex) {
    var group = "[Channel" + deckIndex + "]";
    ["play", "playposition", "rate", "volume"].forEach(function (control) {
        AiDjProbe.sendState(
            deckIndex,
            control,
            engine.getValue(group, control),
            true
        );
    });
    AiDjProbe.sendTrackLoaded(deckIndex, engine.getValue(group, "track_samples"));
    AiDjProbe.sendTrackFingerprint(deckIndex, group);
    var trackSamples = engine.getValue(group, "track_samples");
    if (trackSamples > 0) {
        AiDjProbe.sendPreciseSeekResult(
            deckIndex,
            engine.getValue(group, "playposition") * trackSamples,
            1
        );
    }
    AiDjProbe.sendLoopEnabled(deckIndex, engine.getValue(group, "loop_enabled"));
    AiDjProbe.sendSyncMode(deckIndex, engine.getValue(group, "sync_mode"));
    AiDjProbe.sendChannelShape(
        deckIndex,
        "eq_low",
        engine.getParameter(
            "[EqualizerRack1_" + group + "_Effect1]",
            "parameter1"
        ),
        true
    );
    AiDjProbe.sendChannelShape(
        deckIndex,
        "filter",
        engine.getParameter("[QuickEffectRack1_" + group + "]", "super1"),
        true
    );
    AiDjProbe.sendCrossfader(engine.getValue("[Master]", "crossfader"), true);
};

AiDjProbe.connectDeck = function (deckIndex) {
    var group = "[Channel" + deckIndex + "]";
    ["play", "playposition", "rate", "volume"].forEach(function (control) {
        var connection = engine.makeConnection(
            group,
            control,
            function (value) {
                AiDjProbe.sendState(deckIndex, control, value);
            }
        );
        AiDjProbe.connections.push(connection);
        connection.trigger();
    });
    var trackConnection = engine.makeConnection(
        group,
        "track_samples",
        function (value) {
            AiDjProbe.sendTrackLoaded(deckIndex, value);
            AiDjProbe.sendTrackFingerprint(deckIndex, group);
        }
    );
    AiDjProbe.connections.push(trackConnection);
    trackConnection.trigger();
    var loopConnection = engine.makeConnection(
        group,
        "loop_enabled",
        function (value) {
            AiDjProbe.sendLoopEnabled(deckIndex, value);
        }
    );
    AiDjProbe.connections.push(loopConnection);
    loopConnection.trigger();
    var syncModeConnection = engine.makeConnection(
        group,
        "sync_mode",
        function (value) {
            AiDjProbe.sendSyncMode(deckIndex, value);
        }
    );
    AiDjProbe.connections.push(syncModeConnection);
    syncModeConnection.trigger();
    var eqLowConnection = engine.makeConnection(
        "[EqualizerRack1_" + group + "_Effect1]",
        "parameter1",
        function (value) {
            AiDjProbe.sendChannelShape(deckIndex, "eq_low", value);
        }
    );
    AiDjProbe.connections.push(eqLowConnection);
    eqLowConnection.trigger();
    var filterConnection = engine.makeConnection(
        "[QuickEffectRack1_" + group + "]",
        "super1",
        function (value) {
            AiDjProbe.sendChannelShape(deckIndex, "filter", value);
        }
    );
    AiDjProbe.connections.push(filterConnection);
    filterConnection.trigger();
};

AiDjProbe.init = function () {
    AiDjProbe.connectDeck(1);
    AiDjProbe.connectDeck(2);
    var crossfaderConnection = engine.makeConnection(
        "[Master]",
        "crossfader",
        AiDjProbe.sendCrossfader
    );
    AiDjProbe.connections.push(crossfaderConnection);
    crossfaderConnection.trigger();
    var recordingConnection = engine.makeConnection(
        "[Recording]",
        "status",
        AiDjProbe.sendRecordingStatus
    );
    AiDjProbe.connections.push(recordingConnection);
    recordingConnection.trigger();
};

AiDjProbe.shutdown = function () {
    AiDjProbe.connections.forEach(function (connection) {
        connection.disconnect();
    });
    AiDjProbe.connections = [];
    AiDjProbe.lastMidiValues = {};
    AiDjProbe.preciseSeekParts = {1: [0, 0, 0, 0, 0], 2: [0, 0, 0, 0, 0]};
    AiDjProbe.preciseSeekMasks = {1: 0, 2: 0};
    AiDjProbe.bassTargetDeck = 0;
};
