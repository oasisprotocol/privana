import React from 'react';
import Layout from '@theme/Layout';
import Faq from '@site/src/components/Faq';

export default function FaqPage(): React.ReactNode {
  return (
    <Layout
      title="FAQ"
      description="Answers to common questions about Privana — privacy, swaps, yield, automation, and self-custody on Oasis Sapphire.">
      <main>
        <Faq />
      </main>
    </Layout>
  );
}
