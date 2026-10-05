// Run with: node --test tests/planner.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// The planner lives inside engineer_heatmap.py as PLANNER_JS = r'''...'''
const py = fs.readFileSync(path.join(__dirname, "..", "engineer_heatmap.py"), "utf8");
const src = py.match(/PLANNER_JS = r'''([\s\S]*?)'''/)[1];
const mod = { exports: {} };
new Function("module", src)(mod);
const P = mod.exports;

const settings = {
  min_stop_min: 30, target_office: "06:00", target_site: "07:50",
  target_work_end_weekday: "15:00", target_work_end_friday: "14:00",
  target_home_weekday: "16:00", target_home_friday: "15:00", late_tolerance: 5,
  contract_hours_mon_thu: 10, contract_hours_fri: 9, road_factor: 1.3, avg_speed_kmh: 30,
};
const office = { lat: 51.6, lon: -0.19, prefix: "N3", postcode: "N3" };
// ~0.009 deg lat = 1 km
const at = (km, id) => Object.assign(id ? { id, name: id } : {}, { lat: 51.6 + km * 0.009, lon: -0.19 });
const ctx = { office, settings };

test("time helpers", () => {
  assert.equal(P.toMin("07:50"), 470);
  assert.equal(P.fmt(470), "07:50");
  assert.equal(P.previousWorkingDay("2026-10-05"), "2026-10-02"); // Mon -> Fri
  assert.equal(P.previousWorkingDay("2026-10-07"), "2026-10-06");
  assert.equal(P.dayTargets("2026-10-09", settings).home, P.toMin("15:00")); // Friday
  assert.equal(P.dayTargets("2026-10-08", settings).home, P.toMin("16:00"));
});

test("office prefix rule matches the timekeeping project", () => {
  assert.ok(P.isOffice("N3 1AB", "N3"));
  assert.ok(P.isOffice("n31ab", "N3"));
  assert.ok(!P.isOffice("N4 1AB", "N3"));
  assert.ok(!P.isOffice("", "N3"));
});

test("no materials -> direct, closest engineer wins", () => {
  const job = { id: "j", date: "2026-10-06", postcode: "E1 1AA", needsMaterials: false, ...at(20) };
  const near = at(18, "near"), far = at(5, "far");
  const r = P.rankCandidates(job, [far, near], [], ctx);
  assert.deepEqual(r.ranked.map((c) => c.engineer.id), ["near", "far"]);
  assert.equal(r.ranked[0].best.kind, "direct");
  assert.equal(r.ranked[0].best.arriveSite, P.toMin("07:50"));
});

test("materials: morning office pickup arrives on time when the site is close to the office", () => {
  const job = { id: "j", date: "2026-10-06", postcode: "E1 1AA", needsMaterials: true, ...at(10) };
  const [opt] = P.planOptions(job, at(12, "e"), { ...ctx, prevSite: null });
  assert.equal(opt.kind, "office_morning");
  assert.equal(opt.arriveOffice, P.toMin("06:00"));
  assert.equal(opt.siteLate, 0);
  assert.ok(opt.onTime);
});

test("materials: far site makes the morning pickup late; day-before collection wins", () => {
  // office -> site 60 km ~ 156 min, so leaving at 06:30 arrives 09:06
  const job = { id: "j", date: "2026-10-06", postcode: "RH1 1AA", needsMaterials: true, ...at(60) };
  const eng = at(58, "e");
  const prev = { id: "p", date: "2026-10-05", postcode: "N3 2BB", engineerId: "e", ...at(0.5) };
  const opts = P.planOptions(job, eng, { ...ctx, prevSite: prev });
  const morning = opts.find((o) => o.kind === "office_morning");
  assert.ok(morning.siteLate > 60);
  assert.equal(opts[0].kind, "collect_before");
  assert.equal(opts[0].prevDay.atOffice, true); // already at the office yesterday: no detour
  assert.equal(opts[0].extraMin, 0);
});

test("busy engineers are not ranked, the job's own assignee is", () => {
  const job = { id: "j", date: "2026-10-06", postcode: "E1 1AA", engineerId: "a", needsMaterials: false, ...at(10) };
  const bookings = [
    { ...job },
    { id: "x", date: "2026-10-06", postcode: "SE1 1AA", engineerId: "b", ...at(3) },
  ];
  const r = P.rankCandidates(job, [at(1, "a"), at(1, "b"), { id: "c", name: "c", lat: null }], bookings, ctx);
  assert.deepEqual(r.ranked.map((c) => c.engineer.id), ["a"]);
  assert.ok(r.ranked[0].assigned);
  assert.deepEqual(r.busy.map((b) => b.engineer.id), ["b"]);
  assert.deepEqual(r.unplaceable.map((b) => b.engineer.id), ["c"]);
});

test("same-site continuity is flagged", () => {
  const job = { id: "j", date: "2026-10-06", postcode: "E1 1AA", needsMaterials: false, ...at(10) };
  const bookings = [{ id: "y", date: "2026-10-05", postcode: "E1 1AA", engineerId: "a", ...at(10) }];
  const r = P.rankCandidates(job, [at(2, "a")], bookings, ctx);
  assert.ok(r.ranked[0].sameSiteYesterday);
});

test("long day is reported as overtime against contract hours", () => {
  const job = { id: "j", date: "2026-10-06", postcode: "E1 1AA", needsMaterials: true, ...at(5) };
  const [opt] = P.planOptions(job, at(-40, "e"), { ...ctx, prevSite: null }); // lives 40 km the other side
  assert.equal(opt.kind, "office_morning");
  assert.ok(opt.overtimeMin > 0);
  assert.ok(opt.homeLate > 0);
});

test("board StartTime replaces the default on-site target", () => {
  const job = { id: "j", date: "2026-10-06", postcode: "E1 1AA", needsMaterials: false, startTime: "09:00", ...at(10) };
  const [opt] = P.planOptions(job, at(12, "e"), { ...ctx, prevSite: null });
  assert.equal(opt.arriveSite, P.toMin("09:00"));
});

test("an engineer who is off (holiday) is not ranked", () => {
  const job = { id: "j", date: "2026-10-06", postcode: "E1 1AA", needsMaterials: false, ...at(10) };
  const bookings = [{ id: "h", date: "2026-10-06", postcode: null, off: "Holiday", engineerId: "a" }];
  const r = P.rankCandidates(job, [at(1, "a"), at(2, "b")], bookings, ctx);
  assert.deepEqual(r.ranked.map((c) => c.engineer.id), ["b"]);
});
