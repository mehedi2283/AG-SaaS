"use client";

import Link from "next/link";

export default function RepliesPage() {
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1 className="title">Replies</h1>
          <p className="lede">What buyers write back, with a drafted answer for you to send or change.</p>
        </div>
      </header>
      <section className="empty">
        <p className="letter">Replies from buyers will appear here.</p>
        <p className="muted">
          Gazi reads each reply, works out what the buyer wants and drafts an answer. Interested buyers and
          questions wait for your approval; the rest follow your workspace rules.
        </p>
        <div className="actions">
          <Link className="btn btn-primary" href="/settings">
            Connect Instantly or HeyReach
          </Link>
        </div>
      </section>
    </div>
  );
}
