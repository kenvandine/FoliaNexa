package io.github.kenvandine.campuslobby;

import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.block.Block;
import org.bukkit.block.BlockFace;
import org.bukkit.block.BlockState;
import org.bukkit.block.Sign;
import org.bukkit.block.data.BlockData;
import org.bukkit.block.data.Rotatable;
import org.bukkit.block.sign.Side;
import org.bukkit.block.sign.SignSide;
import org.bukkit.plugin.Plugin;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Places a generated {@link CampusScene.Scene} into a real world. This is
 * the only class in the plugin that touches game state, and it does so
 * Folia-safely: block placements are grouped by the chunk they land in
 * and each chunk's group is scheduled on {@link Bukkit#getRegionScheduler()}
 * against that chunk's coordinates, rather than placed directly from
 * whatever thread the command/listener happened to run on (which under
 * Folia's regionized ticking is not guaranteed to own that chunk's
 * region — see docs/plugin-dev/02-plugin-architecture.md §2.3 in the
 * folia-server repo).
 */
public final class SceneBuilder {

    private static final BlockFace[] SIGN_ROTATIONS = {
            BlockFace.SOUTH, BlockFace.SOUTH_SOUTH_WEST, BlockFace.SOUTH_WEST, BlockFace.WEST_SOUTH_WEST,
            BlockFace.WEST, BlockFace.WEST_NORTH_WEST, BlockFace.NORTH_WEST, BlockFace.NORTH_NORTH_WEST,
            BlockFace.NORTH, BlockFace.NORTH_NORTH_EAST, BlockFace.NORTH_EAST, BlockFace.EAST_NORTH_EAST,
            BlockFace.EAST, BlockFace.EAST_SOUTH_EAST, BlockFace.SOUTH_EAST, BlockFace.SOUTH_SOUTH_EAST
    };

    private SceneBuilder() {
    }

    public static void build(Plugin plugin, World world, Location origin, SceneConfig config) {
        CampusScene.Scene scene = CampusScene.generate(config);

        int originX = origin.getBlockX();
        int originY = origin.getBlockY();
        int originZ = origin.getBlockZ();

        Map<Long, List<BlockPlacement>> byChunk = new HashMap<>();
        for (BlockPlacement placement : scene.blocks()) {
            int worldX = originX + placement.dx();
            int worldZ = originZ + placement.dz();
            byChunk.computeIfAbsent(chunkKey(worldX >> 4, worldZ >> 4), key -> new ArrayList<>()).add(placement);
        }

        for (List<BlockPlacement> placements : byChunk.values()) {
            BlockPlacement first = placements.get(0);
            int chunkX = (originX + first.dx()) >> 4;
            int chunkZ = (originZ + first.dz()) >> 4;
            Bukkit.getRegionScheduler().run(plugin, world, chunkX, chunkZ, task ->
                    placements.forEach(p -> placeBlock(world, originX + p.dx(), originY + p.dy(), originZ + p.dz(), p.material())));
        }

        for (SignPlacement sign : scene.signs()) {
            int worldX = originX + sign.dx();
            int worldY = originY + sign.dy();
            int worldZ = originZ + sign.dz();
            Bukkit.getRegionScheduler().run(plugin, world, worldX >> 4, worldZ >> 4, task ->
                    placeSign(world, worldX, worldY, worldZ, sign));
        }
    }

    private static void placeBlock(World world, int x, int y, int z, String materialName) {
        Material material = Material.matchMaterial(materialName);
        if (material == null) {
            // Unknown/misspelled material name from an operator-edited
            // config.yml — skip it rather than crash the rest of the build.
            return;
        }
        world.getBlockAt(x, y, z).setType(material, false);
    }

    private static void placeSign(World world, int x, int y, int z, SignPlacement placement) {
        Block block = world.getBlockAt(x, y, z);
        block.setType(Material.OAK_SIGN, false);

        BlockData data = block.getBlockData();
        if (data instanceof Rotatable rotatable) {
            int index = Math.floorMod(placement.rotation(), SIGN_ROTATIONS.length);
            rotatable.setRotation(SIGN_ROTATIONS[index]);
            block.setBlockData(rotatable, false);
        }

        BlockState state = block.getState();
        if (!(state instanceof Sign sign)) {
            return;
        }
        SignSide front = sign.getSide(Side.FRONT);
        List<String> lines = placement.lines();
        for (int i = 0; i < lines.size() && i < 4; i++) {
            front.line(i, Component.text(lines.get(i)));
        }
        sign.update(true, false);
    }

    private static long chunkKey(int chunkX, int chunkZ) {
        return (((long) chunkX) << 32) ^ (chunkZ & 0xffffffffL);
    }
}
