"use client";

import { useEffect, useState } from "react";
import { duration, getServices, rupees, type Service } from "@/lib/api";

// Shown like the printed price list on a salon wall: name, dotted leader, duration, price.
export default function ServiceMenu() {
  const [services, setServices] = useState<Service[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    getServices()
      .then(setServices)
      .catch(() => setFailed(true));
  }, []);

  return (
    <section aria-labelledby="services-heading">
      <h2 id="services-heading" className="font-display text-2xl font-semibold">
        Services
      </h2>

      {failed && (
        <p className="mt-4 text-muted">
          The price list didn&apos;t load. Check your connection and refresh the page.
        </p>
      )}

      {!services && !failed && (
        <p className="mt-4 text-muted" aria-live="polite">
          Loading prices…
        </p>
      )}

      {services && (
        <ul className="mt-5 space-y-4">
          {services.map((s) => (
            <li key={s.id} className="flex items-baseline gap-3">
              <span className="text-lg font-semibold">{s.name}</span>
              <span aria-hidden className="flex-1 translate-y-[-4px] border-b-2 border-dotted border-line" />
              <span className="text-muted whitespace-nowrap">{duration(s.duration_min)}</span>
              <span className="w-20 text-right font-semibold tabular-nums">{rupees(s.price)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}