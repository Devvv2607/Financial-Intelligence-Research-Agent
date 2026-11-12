"use client";

type SectorChipsProps = {
  sectors: string[];
  selected: string | null;
  onSelect: (sector: string | null) => void;
};

export function SectorChips({ sectors, selected, onSelect }: SectorChipsProps) {
  return (
    <div className="flex flex-wrap gap-2" aria-label="Filter companies by sector">
      <button
        type="button"
        aria-pressed={selected === null}
        onClick={() => onSelect(null)}
        className={`rounded-md border px-2.5 py-1 text-xs ${
          selected === null
            ? "border-accent text-foreground"
            : "border-border text-muted hover:border-accent hover:text-foreground"
        }`}
      >
        All sectors
      </button>
      {sectors.map((sector) => (
        <button
          key={sector}
          type="button"
          aria-pressed={selected === sector}
          onClick={() => onSelect(sector)}
          className={`rounded-md border px-2.5 py-1 text-xs ${
            selected === sector
              ? "border-accent text-foreground"
              : "border-border text-muted hover:border-accent hover:text-foreground"
          }`}
        >
          {sector}
        </button>
      ))}
    </div>
  );
}
