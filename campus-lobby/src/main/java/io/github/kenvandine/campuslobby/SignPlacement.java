package io.github.kenvandine.campuslobby;

import java.util.List;

/**
 * A standing sign to place, in coordinates relative to the scene origin.
 * {@code rotation} is a raw Minecraft sign rotation value (0-15, matching
 * the block's {@code Rotatable} data — 0 = south, 4 = west, 8 = north,
 * 12 = east). {@code lines} holds up to 4 lines of sign text.
 */
public record SignPlacement(int dx, int dy, int dz, int rotation, List<String> lines) {
}
