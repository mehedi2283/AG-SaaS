/* Loading placeholders shaped like the real screens: titles, hairline rows, letters. They fade in after a short
   delay (globals.css), so a fast load never flashes them. */

const ROW_WIDTHS = ["42%", "58%", "36%", "51%", "46%", "62%"];

function Bar({ w, h = 12 }: { w: string; h?: number }) {
  return <span className="sk-bar" style={{ width: w, height: h }} />;
}

/** Rows divided by hairlines, like the tables and settings lists. */
export function SkeletonRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="sk rows" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div className="sk-row" key={i}>
          <div className="sk-stack">
            <Bar w={ROW_WIDTHS[i % ROW_WIDTHS.length]} h={13} />
            <Bar w="24%" h={10} />
          </div>
          <Bar w="64px" h={10} />
        </div>
      ))}
    </div>
  );
}

/** Messages: serif-height lines beside the same left rule as a real message. */
export function SkeletonLetters({ count = 2 }: { count?: number }) {
  return (
    <div className="sk" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <div className="sk-thread" key={i}>
          <Bar w="30%" h={13} />
          <div className="sk-letter">
            <Bar w="38%" h={12} />
            <Bar w="92%" h={10} />
            <Bar w="86%" h={10} />
            <Bar w={i % 2 ? "54%" : "71%"} h={10} />
          </div>
        </div>
      ))}
    </div>
  );
}

/** A whole page: title, one line of description, then rows. Used while a screen opens. */
export function SkeletonPage({ rows = 5 }: { rows?: number }) {
  return (
    <div className="page" aria-busy="true">
      <span className="sr-only" role="status">
        Loading
      </span>
      <div className="sk sk-head" aria-hidden="true">
        <Bar w="220px" h={26} />
        <Bar w="min(420px, 80%)" h={12} />
      </div>
      <SkeletonRows rows={rows} />
    </div>
  );
}

/** The app frame before the workspace is known: the side rail and an opening page. */
export function SkeletonShell() {
  return (
    <div className="shell" aria-busy="true">
      <aside className="rail" aria-hidden="true">
        <span className="wordmark">Gazi</span>
        <div className="sk sk-rail">
          <Bar w="100%" h={44} />
          {["62%", "70%", "48%", "58%"].map((w) => (
            <Bar key={w} w={w} h={12} />
          ))}
        </div>
      </aside>
      <main className="main">
        <SkeletonPage />
      </main>
    </div>
  );
}
