-- A stock: units on hand for a few tracks, snapshotted daily.
-- chinook ships no level or balance column, so this is added to give `stock`
-- real data to be measured against. Small on purpose: every figure the tests
-- assert is computable by hand from these rows.
--
-- Re-runnable: the CREATE is IF NOT EXISTS and the TRUNCATE makes the INSERT
-- idempotent, so seeding twice leaves the same eight rows.
--
-- The primary key is (track_id, as_of_date) rather than a surrogate, and it is
-- also the index a stock window wants: the boundary is computed with
-- `max(as_of_date) over (partition by <group keys>)`, so an index leading with
-- the group keys and ending in the time column lets the planner feed the
-- WindowAgg pre-sorted instead of sorting the whole population. See
-- `tools/bench.py`'s WINDOW shape for what that sort costs when it is absent.
CREATE TABLE IF NOT EXISTS daily_inventory (
    track_id     integer NOT NULL REFERENCES track (track_id),
    as_of_date   date    NOT NULL,
    units_on_hand integer NOT NULL,
    PRIMARY KEY (track_id, as_of_date)
);

TRUNCATE daily_inventory;

INSERT INTO daily_inventory (track_id, as_of_date, units_on_hand) VALUES
  -- track 1: rises then falls. Latest (03) = 4, earliest (01) = 10.
  (1, '2026-01-01', 10), (1, '2026-01-02', 25), (1, '2026-01-03', 4),
  -- track 2: latest (03) = 7, earliest (01) = 3.
  (2, '2026-01-01', 3),  (2, '2026-01-02', 5),  (2, '2026-01-03', 7),
  -- track 3: TIE on the latest date with tracks 1 and 2, so the window must
  -- keep BOTH rows at that instant rather than picking one.
  (3, '2026-01-03', 100),
  -- track 4: only ever recorded earlier, so it is absent at the latest instant
  -- and must NOT contribute to a `last` window.
  (4, '2026-01-01', 999);

-- Hand-computed: sum of units at the LAST instant (2026-01-03) = 4 + 7 + 100
--                = 111.  Sum at the FIRST instant (2026-01-01) = 10 + 3 + 999
--                = 1012.  Naive sum across all dates = 1153.
