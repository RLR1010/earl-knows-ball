import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Privacy Policy — Earl Knows Ball",
};

export default function PrivacyPage() {
  return (
    <div className="max-w-3xl mx-auto px-4 py-12">
      <h1 className="text-3xl font-bold mb-2">Privacy Policy</h1>
      <p className="text-sm text-gray-500 mb-8">Last updated: July 31, 2026</p>

      <p className="text-gray-400 mb-8">
        This Privacy Policy describes how Earl Knows Ball, operated by{" "}
        <strong className="text-gray-200">Nexmuse, LLC</strong>, collects, uses, and shares
        information about you when you visit or use earlknowsball.com (the &ldquo;Service&rdquo;).
        By using the Service, you agree to this Privacy Policy and to the storage of cookies in your
        browser as described below.
      </p>

      <section className="mb-8">
        <h2 className="text-xl font-semibold mb-3 text-white">1. Information We Collect</h2>

        <h3 className="text-lg font-medium mb-2 text-gray-200">1.1. Information You Provide</h3>
        <p className="text-gray-400">
          We collect information you provide directly to us, including when you create an account,
          purchase a subscription, contact support, or otherwise communicate with us. This may
          include your name, email address, payment card information (processed by our payment
          provider, Stripe), and any other information you choose to provide.
        </p>

        <h3 className="text-lg font-medium mt-5 mb-2 text-gray-200">
          1.2. Information We Collect Automatically
        </h3>
        <p className="text-gray-400">
          When you visit the Service, we may automatically collect certain information about your
          device and usage, including your IP address, browser type, operating system, pages
          viewed, and the dates and times of your visits. We use this information to operate,
          maintain, and improve the Service, and to help us understand how users interact with it.
        </p>
      </section>

      <section className="mb-8">
        <h2 className="text-xl font-semibold mb-3 text-white">2. How We Use Your Data</h2>

        <h3 className="text-lg font-medium mb-2 text-gray-200">2.1. Provision of the Service</h3>
        <p className="text-gray-400">
          We use the information we collect to provide, maintain, and improve the Service, to
          process transactions and manage your account, and to respond to your comments, questions,
          and requests.
        </p>

        <h3 className="text-lg font-medium mt-5 mb-2 text-gray-200">2.2. Communications</h3>
        <p className="text-gray-400">
          We may use your information to send you service-related communications, including
          account notices, billing receipts, and updates to our policies and terms. If you opt in
          to receive marketing communications, we may send you emails about our services.
        </p>

        <h3 className="text-lg font-medium mt-5 mb-2 text-gray-200">2.3. Legal and Regulatory</h3>
        <p className="text-gray-400">
          We may use your information to meet legal and regulatory requirements, including
          compliance with applicable laws and the enforcement of our Terms of Service.
        </p>

        <h3 className="text-lg font-medium mt-5 mb-2 text-gray-200">2.4. Cookies</h3>
        <p className="text-gray-400">
          We use cookies and similar technologies to keep you signed in, remember your
          preferences, and understand how the Service is used. You can control cookies through
          your browser settings; however, disabling cookies may affect your ability to use certain
          features of the Service.
        </p>
      </section>

      <section className="mb-8">
        <h2 className="text-xl font-semibold mb-3 text-white">3. Sharing Your Information</h2>

        <h3 className="text-lg font-medium mb-2 text-gray-200">
          3.1. Third-Party Service Providers
        </h3>
        <p className="text-gray-400">
          We share some of your data with third-party service providers who help us operate the
          Service, including:
        </p>
        <ul className="list-disc ml-6 mt-2 text-gray-400 space-y-1">
          <li>
            <strong className="text-gray-200">Stripe</strong> — processes payments for premium
            subscriptions. Stripe receives your payment card information and billing details to
            complete transactions. Please review{" "}
            <a
              href="https://stripe.com/privacy"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-400 hover:underline"
            >
              Stripe&rsquo;s Privacy Policy
            </a>{" "}
            for more information.
          </li>
          <li>
            <strong className="text-gray-200">Hosting and infrastructure providers</strong> — we
            use third-party hosting providers to store data and serve the Service.
          </li>
        </ul>
        <p className="text-gray-400 mt-3">
          These providers are authorized to use your personal information only as necessary to
          provide services to us.
        </p>

        <h3 className="text-lg font-medium mt-5 mb-2 text-gray-200">
          3.2. Legal Compliance and Protection
        </h3>
        <p className="text-gray-400">
          We may disclose any information we collect about you, whether you are a current or former
          customer, with law enforcement, data protection authorities, government officials, and
          other authorities, when:
        </p>
        <ul className="list-disc ml-6 mt-2 text-gray-400 space-y-1">
          <li>compelled by subpoena, court order, or other legal procedure;</li>
          <li>we believe the disclosure is necessary to prevent physical harm or financial loss;</li>
          <li>disclosure is necessary to report suspected illegal activity;</li>
          <li>disclosure is necessary to investigate violations of this Privacy Policy or our Terms of Service;</li>
          <li>we obtain your consent or act at your direction.</li>
        </ul>
        <p className="text-gray-400 mt-3">
          Other than in connection with a merger, sale of Nexmuse, LLC&rsquo;s assets, financing, or
          acquisition, we will not sell or rent any of your information to third parties for their
          own marketing purposes.
        </p>
      </section>

      <section className="mb-8">
        <h2 className="text-xl font-semibold mb-3 text-white">4. Legal Basis for Processing Data</h2>
        <p className="text-gray-400">
          We collect, use, and share data as described above. We will only collect and process
          personal data about you where we have lawful bases. Lawful bases include consent (where
          you have given consent), contract (where processing is necessary for the performance of a
          contract with you), and legitimate interests (where processing is necessary for our
          legitimate business interests, such as improving and securing the Service).
        </p>
      </section>

      <section className="mb-8">
        <h2 className="text-xl font-semibold mb-3 text-white">5. Your Choices and Obligations</h2>

        <h3 className="text-lg font-medium mb-2 text-gray-200">5.1. Data Retention</h3>
        <p className="text-gray-400">
          We retain personal data for as long as necessary to fulfill the purposes described in this
          Privacy Policy, subject to our own legal and regulatory obligations. In accordance with
          our record-keeping obligations, we will retain basic account information and information
          about performed transactions for a reasonable period after an account is closed.
        </p>

        <h3 className="text-lg font-medium mt-5 mb-2 text-gray-200">
          5.2. Accessing and Controlling Your Personal Data
        </h3>
        <p className="text-gray-400">
          Regarding your personal data, you have the following options:
        </p>
        <ul className="list-disc ml-6 mt-2 text-gray-400 space-y-1">
          <li>
            <strong className="text-gray-200">Delete data:</strong> You can request deletion of
            your personal data that we have about you. We will delete the data that we are not
            legally obliged to keep. Since some of the data is necessary to provide the Service to
            you, you will not be able to use our services after the deletion.
          </li>
          <li>
            <strong className="text-gray-200">Change or correct data:</strong> You have the right to
            request a change of incorrect personal data that we have about you.
          </li>
          <li>
            <strong className="text-gray-200">Object to, limit, or restrict use of data:</strong>{" "}
            You can request that we stop using all or some of your personal data (e.g., if we have
            no legal right to keep using it) or limit our use of it (e.g., if your personal data is
            inaccurate or unlawfully held).
          </li>
        </ul>
        <p className="text-gray-400 mt-3">
          You can make a request for any of the above actions by contacting us through the site.
        </p>

        <h3 className="text-lg font-medium mt-5 mb-2 text-gray-200">
          5.3. Account Information and Account Deletion
        </h3>
        <p className="text-gray-400">
          You may access, review, or update your online account information at any time by logging
          into your account. If you choose to close your account, please contact us through the
          site. Your account will be deleted and your personal data will be erased within 30 days of
          receiving your request.
        </p>
        <p className="text-gray-400 mt-3">
          We retain your personal data even after you have closed your account if reasonably
          necessary to comply with our legal obligations, meet regulatory requirements, resolve
          disputes, maintain security, prevent fraud and abuse, or enforce our Terms of Service. We
          will retain de-personalized information after your account has been deleted.
        </p>
      </section>

      <section className="mb-8">
        <h2 className="text-xl font-semibold mb-3 text-white">6. Security</h2>
        <p className="text-gray-400">
          We implement reasonable security practices and procedures to help protect the
          confidentiality and security of your information, including any non-public personal
          information. We protect your information using reasonable physical, technical, and
          administrative security measures, including by limiting access to your information to
          personnel with a need to know such information. However, no method of transmission over
          the internet or method of electronic storage is completely secure, and we cannot
          guarantee absolute security.
        </p>
      </section>

      <section className="mb-8">
        <h2 className="text-xl font-semibold mb-3 text-white">7. Contact Us</h2>
        <p className="text-gray-400">
          If you have any questions or concerns regarding this Privacy Policy, please contact us
          through the site.
        </p>
      </section>
    </div>
  );
}
