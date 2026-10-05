/* Candidate ranking for a job: who should go, and how do they get the materials?
 *
 * For each free engineer we cost three ways of doing the day and keep the best:
 *   direct          home -> site -> home (no materials needed, or the site is the office)
 *   office_morning  home -> office (06:00, min stop) -> site (07:50) -> home
 *   collect_before  on the previous working day: last site -> office -> home,
 *                   then straight home -> site on the job day
 * Lateness against the targets (beyond the tolerance) is penalised, so an
 * on-time option always beats a late one of similar mileage.
 *
 * Plain functions, no DOM - shared by the page and the node tests.
 */
(function (root) {
  "use strict";

  const LATE_SITE_WEIGHT = 3; // a minute late on site costs as much as 3 minutes of driving
  const LATE_HOME_WEIGHT = 1;

  function toMin(hhmm) {
    const [h, m] = String(hhmm).split(":").map(Number);
    return h * 60 + (m || 0);
  }

  function fmt(min) {
    const m = Math.round(min);
    const h = Math.floor(m / 60);
    return `${String(((h % 24) + 24) % 24).padStart(2, "0")}:${String(((m % 60) + 60) % 60).padStart(2, "0")}`;
  }

  function haversineKm(a, b) {
    const R = 6371;
    const rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad;
    const dLon = (b.lon - a.lon) * rad;
    const s = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(s));
  }

  function travelMin(a, b, s) {
    return (haversineKm(a, b) * s.road_factor / s.avg_speed_kmh) * 60;
  }

  function normPc(pc) {
    return String(pc || "").replace(/\s+/g, "").toUpperCase();
  }

  function isOffice(pc, prefix) {
    return !!pc && !!prefix && normPc(pc).startsWith(normPc(prefix));
  }

  function parseDate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  }

  function isoDate(d) {
    return d.toISOString().slice(0, 10);
  }

  function previousWorkingDay(iso) {
    const d = parseDate(iso);
    do d.setUTCDate(d.getUTCDate() - 1); while (d.getUTCDay() === 0 || d.getUTCDay() === 6);
    return isoDate(d);
  }

  /** Targets for a date: Friday has its own finish and home times + contract hours. */
  function dayTargets(iso, s) {
    const fri = parseDate(iso).getUTCDay() === 5;
    return {
      friday: fri,
      office: toMin(s.target_office),
      site: toMin(s.target_site),
      workEnd: toMin(fri ? s.target_work_end_friday : s.target_work_end_weekday),
      home: toMin(fri ? s.target_home_friday : s.target_home_weekday),
      contractMin: (fri ? s.contract_hours_fri : s.contract_hours_mon_thu) * 60,
      tol: Number(s.late_tolerance),
      minStop: Number(s.min_stop_min),
    };
  }

  function over(actual, target, tol) {
    const late = actual - target;
    return late > tol ? late : 0;
  }

  /**
   * Cost every way this engineer could do the job and return them, best first.
   * ctx: { office:{lat,lon,prefix}, settings, prevSite: booking|null }
   */
  function planOptions(job, engineer, ctx) {
    const s = ctx.settings;
    const t = dayTargets(job.date, s);
    const home = engineer, site = job, office = ctx.office;
    const hs = travelMin(home, site, s);
    const sh = hs;
    const needs = !!job.needsMaterials && !isOffice(job.postcode, office.prefix);
    const opts = [];

    // Job-day afternoon is the same in every option.
    const arriveHome = t.workEnd + sh;
    const homeLate = over(arriveHome, t.home, t.tol);

    const direct = (kind, extra) => {
      const leaveHome = t.site - hs;
      const o = {
        kind,
        legs: [["home", "site", hs], ["site", "home", sh]],
        leaveHome, arriveOffice: null, arriveSite: t.site, arriveHome,
        siteLate: 0, homeLate,
        driveMin: hs + sh,
        dayMin: arriveHome - leaveHome,
        prevDay: null,
      };
      return Object.assign(o, extra || {});
    };

    if (!needs) {
      opts.push(direct("direct"));
    } else {
      // 1. Morning pickup at the office.
      const ho = travelMin(home, office, s);
      const os = travelMin(office, site, s);
      const leaveOffice = Math.max(t.office + t.minStop, t.site - os);
      const arriveSite = leaveOffice + os;
      opts.push({
        kind: "office_morning",
        legs: [["home", "office", ho], ["office", "site", os], ["site", "home", sh]],
        leaveHome: t.office - ho, arriveOffice: t.office, arriveSite, arriveHome,
        siteLate: over(arriveSite, t.site, t.tol), homeLate,
        driveMin: ho + os + sh,
        dayMin: arriveHome - (t.office - ho),
        prevDay: null,
      });

      // 2. Collect on the way home the working day before, then go straight to site.
      const prev = ctx.prevSite;
      if (prev && prev.lat != null) {
        const pt = dayTargets(prev.date, s);
        const prevAtOffice = isOffice(prev.postcode, office.prefix);
        const ph = travelMin(prev, home, s);
        const po = prevAtOffice ? 0 : travelMin(prev, office, s);
        const oh = travelMin(office, home, s);
        const prevHome = prevAtOffice ? pt.workEnd + oh : pt.workEnd + po + t.minStop + oh;
        // Only the lateness the detour adds counts against this option.
        const addedLate = prevAtOffice ? 0 : Math.max(0, over(prevHome, pt.home, pt.tol) - over(pt.workEnd + ph, pt.home, pt.tol));
        opts.push(direct("collect_before", {
          driveMin: hs + sh + (prevAtOffice ? 0 : po + oh - ph),
          prevDay: {
            date: prev.date, postcode: prev.postcode, atOffice: prevAtOffice,
            arriveHome: prevHome, homeTarget: pt.home, addedLate,
            legs: prevAtOffice ? [] : [["prev", "office", po], ["office", "home", oh]],
          },
        }));
      }
    }

    for (const o of opts) {
      const prevLate = o.prevDay ? o.prevDay.addedLate : 0;
      o.contractMin = t.contractMin;
      o.overtimeMin = Math.max(0, o.dayMin - t.contractMin);
      o.extraMin = Math.max(0, o.driveMin - (hs + sh)); // driving added by the materials pickup
      o.onTime = o.siteLate === 0 && o.homeLate === 0 && prevLate === 0;
      o.score = o.driveMin + LATE_SITE_WEIGHT * o.siteLate + LATE_HOME_WEIGHT * (o.homeLate + prevLate);
    }
    opts.sort((a, b) => a.score - b.score);
    return opts;
  }

  /** Index bookings by date -> engineerId -> [bookings]. */
  function indexBookings(bookings) {
    const idx = new Map();
    for (const b of bookings) {
      if (!b.engineerId) continue;
      if (!idx.has(b.date)) idx.set(b.date, new Map());
      const day = idx.get(b.date);
      if (!day.has(b.engineerId)) day.set(b.engineerId, []);
      day.get(b.engineerId).push(b);
    }
    return idx;
  }

  /**
   * Rank engineers for a job. Engineers already booked elsewhere that day are
   * returned separately as `busy` (the job's own assignee is always ranked).
   */
  function rankCandidates(job, engineers, bookings, ctx) {
    const idx = ctx.index || indexBookings(bookings);
    const today = idx.get(job.date) || new Map();
    const prevDate = previousWorkingDay(job.date);
    const prevDay = idx.get(prevDate) || new Map();
    const ranked = [], busy = [], unplaceable = [];

    for (const e of engineers) {
      const own = (today.get(e.id) || []);
      const elsewhere = own.filter((b) => b.id !== job.id && normPc(b.postcode) !== normPc(job.postcode));
      const assigned = job.engineerId === e.id;
      if (elsewhere.length && !assigned) {
        busy.push({ engineer: e, bookings: elsewhere });
        continue;
      }
      if (e.lat == null) {
        unplaceable.push({ engineer: e });
        continue;
      }
      const prevBookings = (prevDay.get(e.id) || []).filter((b) => b.lat != null);
      const prevSite = prevBookings.length ? prevBookings[prevBookings.length - 1] : null;
      const options = planOptions(job, e, Object.assign({}, ctx, { prevSite }));
      ranked.push({
        engineer: e,
        assigned,
        sameSiteYesterday: !!prevSite && normPc(prevSite.postcode) === normPc(job.postcode),
        prevSite,
        best: options[0],
        options,
      });
    }
    ranked.sort((a, b) => a.best.score - b.best.score || a.engineer.name.localeCompare(b.engineer.name));
    return { ranked, busy, unplaceable, prevDate };
  }

  const api = {
    toMin, fmt, haversineKm, travelMin, isOffice, previousWorkingDay, dayTargets,
    planOptions, indexBookings, rankCandidates,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Planner = api;
})(this);
