import React, {useEffect, useState} from 'react';
import clsx from 'clsx';
import {FAQ_CATEGORIES, type FaqItem} from './faqData';
import styles from './styles.module.css';

function Answer({item}: {item: FaqItem}): React.ReactNode {
  // Plain-string answers get wrapped in a paragraph; JSX answers render as-is.
  return typeof item.a === 'string' ? <p>{item.a}</p> : item.a;
}

function FaqEntry({item}: {item: FaqItem}): React.ReactNode {
  return (
    <details className={styles.item} name="faq">
      <summary className={styles.summary}>
        <span>{item.q}</span>
        <span className={styles.icon} aria-hidden="true" />
      </summary>
      <div className={styles.answer}>
        <Answer item={item} />
      </div>
    </details>
  );
}

/** Highlights the rail link for the section currently in view, from scroll position. */
function useScrollSpy(): string {
  const [active, setActive] = useState(FAQ_CATEGORIES[0].id);

  useEffect(() => {
    const ids = FAQ_CATEGORIES.map((c) => c.id);
    const OFFSET = 140; // sticky navbar height + breathing room

    let raf = 0;
    const compute = () => {
      raf = 0;
      let current = ids[0];
      for (const id of ids) {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top - OFFSET <= 0) {
          current = id;
        }
      }
      // Pin the last section when scrolled to the bottom.
      const atBottom =
        window.innerHeight + window.scrollY >=
        document.documentElement.scrollHeight - 2;
      setActive(atBottom ? ids[ids.length - 1] : current);
    };

    const onScroll = () => {
      if (raf === 0) {
        raf = window.requestAnimationFrame(compute);
      }
    };

    compute();
    window.addEventListener('scroll', onScroll, {passive: true});
    window.addEventListener('resize', onScroll, {passive: true});
    return () => {
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
      if (raf) {
        window.cancelAnimationFrame(raf);
      }
    };
  }, []);

  return active;
}

export default function Faq(): React.ReactNode {
  const activeId = useScrollSpy();

  return (
    <div className={styles.container}>
      <header className={styles.hero}>
        <h1 className={styles.heroTitle}>Frequently asked questions</h1>
      </header>

      <div className={styles.body}>
        <div className={styles.content}>
          {FAQ_CATEGORIES.map((category) => (
            <section key={category.id} id={category.id} className={styles.section}>
              <h2 className={styles.sectionTitle}>{category.label}</h2>
              <div className={styles.items}>
                {category.items.map((item) => (
                  <FaqEntry key={item.q} item={item} />
                ))}
              </div>
            </section>
          ))}
        </div>

        <aside className={styles.rail} aria-label="FAQ categories">
          <p className={styles.railTitle}>Categories</p>
          <nav className={styles.railNav}>
            {FAQ_CATEGORIES.map((category) => (
              <a
                key={category.id}
                href={`#${category.id}`}
                className={clsx(
                  styles.railLink,
                  activeId === category.id && styles.railLinkActive,
                )}>
                {category.label}
              </a>
            ))}
          </nav>
        </aside>
      </div>
    </div>
  );
}
