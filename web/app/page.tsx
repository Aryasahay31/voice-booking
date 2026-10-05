import ChairDay from "./components/ChairDay";
import ServiceMenu from "./components/ServiceMenu";
import TalkToShanaya from "./components/TalkToShanaya";

export default function Home() {
  return (
    <>
      <header className="mx-auto flex w-full max-w-6xl flex-wrap items-baseline justify-between gap-2 px-6 pt-8">
        <p className="font-display text-2xl font-bold tracking-tight">Glow Studio</p>
        <p className="text-muted">Open Monday to Saturday, 10 am to 7 pm</p>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-6">
        {/* Hero: the promise on the left, the call itself on the right. */}
        <section className="grid items-center gap-12 py-16 sm:py-24 lg:grid-cols-[1fr_auto]">
          <div className="max-w-3xl">
            <h1 className="font-display text-5xl font-bold leading-[1.05] tracking-tight sm:text-6xl">
              Book your next appointment by talking to Shanaya.
            </h1>
            <p className="mt-6 max-w-[60ch] text-lg leading-relaxed text-muted">
              Shanaya is our AI receptionist. Tell her what you&apos;d like and when, and she&apos;ll
              find a time, read the details back to you, and book it. She can move or cancel an
              appointment too.
            </p>
          </div>
          <TalkToShanaya />
        </section>

        <div className="grid gap-16 pb-20 lg:grid-cols-[1fr_1.1fr]">
          <ServiceMenu />
          <ChairDay />
        </div>
      </main>

      <footer className="border-t border-line">
        <p className="mx-auto max-w-6xl px-6 py-6 text-sm text-muted">
          Calls with Shanaya may be recorded and transcribed to help us improve. She tells you this
          at the start of every call.
        </p>
      </footer>
    </>
  );
}