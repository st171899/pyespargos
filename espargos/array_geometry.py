"""Planar array layouts and reference cabling shared by RX and TX.

Layout cells use the hardware antenna spacing; this is not an arbitrary 3D
position model. Custom apertures can use explicit per-antenna steering weights
and keep their physical coordinates in the application. Reference cable delays
are independent of antenna positions.
"""

import copy
from pathlib import Path
import numpy as np
import yaml
from .combined_array import parse_combined_array_config
from .constants import ANTENNA_SEPARATION, ANTENNAS_PER_BOARD, ANTENNAS_PER_ROW, ROWS_PER_BOARD


class ArrayGeometry:
    def __init__(self, hosts=None, config=None):
        self.cables = self.velocity_factors = None
        self.orientations = None
        self.groups = {}
        self.reference_master = None
        if config:
            data = config if isinstance(config, dict) else yaml.safe_load(Path(config).read_text())["combined-array"]
            data = copy.deepcopy(data)
            for board in data["boards"].values():
                cable = board.setdefault("cable", {})
                cable.setdefault("length", 0.0)
                cable.setdefault("velocity_factor", 1.0)
            self.names = list(data["boards"])
            self.hosts = [b["host"] for b in data["boards"].values()]
            self.reference_master = data.get("reference-master")
            if self.reference_master is not None and self.reference_master not in self.names:
                raise ValueError("reference-master must name a configured board")
            if "groups" in data:
                used = set()
                for name, group in data["groups"].items():
                    members = list(group["boards"])
                    if not members or len(set(members)) != len(members) or used.intersection(members):
                        raise ValueError("Aperture groups must contain distinct, non-overlapping boards")
                    if any(m not in data["boards"] for m in members):
                        raise ValueError("Unknown board in aperture group")
                    subset = {"boards": {m: data["boards"][m] for m in members}, "array": group["array"]}
                    grid, _, _, _, _, _, orientation = parse_combined_array_config(subset)
                    remap = np.array([self.names.index(m)*8+a for m in members for a in range(8)])
                    self.groups[name] = {"boards": tuple(self.names.index(m) for m in members),
                                         "indices": remap[grid], "orientations": orientation}
                    used.update(members)
                if used != set(self.names):
                    raise ValueError("Every configured board must belong to one aperture group")
                primary = data.get("primary-group", next(iter(self.groups)))
                self.indices = self.groups[primary]["indices"]
                self.orientations = self.groups[primary]["orientations"]
                self.cables = np.array([b["cable"]["length"] for b in data["boards"].values()], float)
                self.velocity_factors = np.array([b["cable"]["velocity_factor"] for b in data["boards"].values()], float)
            else:
                self.indices, _, self.cables, self.velocity_factors, _, _, self.orientations = parse_combined_array_config(data)
            if hosts:
                if len(hosts) != len(self.hosts):
                    raise ValueError("Host overrides must match the number of boards in the layout")
                self.hosts = hosts
        else:
            self.hosts = hosts or []
            self.names = list(self.hosts)
            self.indices = np.arange(ANTENNAS_PER_BOARD).reshape(ROWS_PER_BOARD, ANTENNAS_PER_ROW) if len(self.hosts) == 1 else None
        if not self.hosts or any(not isinstance(h, str) or not h.strip() for h in self.hosts) or len(set(self.hosts)) != len(self.hosts):
            raise ValueError("Provide distinct, nonempty board addresses")

    def positions(self, boards):
        """Outward-looking x/y coordinates, indexed by board and antenna ID."""
        if self.indices is None:
            raise ValueError("Beam steering across boards requires antenna positions; use Pool.from_config()")
        result = np.zeros((len(boards), ANTENNAS_PER_BOARD, 2))
        grids = [g["indices"] for g in self.groups.values()] if self.groups else [self.indices]
        for grid in grids:
            rows, cols = grid.shape
            for row, col in np.ndindex(grid.shape):
                board, local = divmod(int(grid[row, col]), ANTENNAS_PER_BOARD)
                for a in range(ANTENNAS_PER_BOARD):
                    if boards[board].revision.antenna_id_to_row_col(a) == divmod(local, ANTENNAS_PER_ROW):
                        result[board, a] = ((cols - 1) / 2 - col, (rows - 1) / 2 - row)
        return result * ANTENNA_SEPARATION
