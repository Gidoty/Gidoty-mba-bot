// The three approved legal documents, reproduced verbatim (not
// paraphrased) and structured only enough for LegalPage.jsx to render
// them - each section is { heading, body }, where body is a list of
// either plain paragraph strings or { list: [...] } for bullet lists.
// There is deliberately no fourth document here - no Refund Policy
// exists, is linked, or is implied anywhere in this app.

export const TERMS_OF_SERVICE = {
  title: "Terms of Service",
  effectiveDate: "18 October 2026",
  intro: [
    'These Terms of Service ("Terms") govern access to and use of the Metabridge Academy automated cryptocurrency trading bot, website, dashboard, APIs, notifications, and related services (collectively, the "Service").',
    "By creating an account, purchasing access, connecting an exchange account, enabling live trading, or otherwise using the Service, you acknowledge that you have read, understood, and agreed to these Terms.",
  ],
  sections: [
    {
      heading: "1. Nature of the Service",
      body: [
        "The Metabridge Academy Trading Bot is a technology platform that provides tools for automated cryptocurrency trading, including strategy execution, exchange connectivity, trading-account configuration, testnet functionality, trade monitoring, notifications, and related functionality.",
        "Metabridge Academy does not guarantee trading profits, investment returns, or particular trading outcomes.",
        "The Service should not be interpreted as a guarantee that any particular cryptocurrency, trading strategy, market, pair, or position will be profitable.",
      ],
    },
    {
      heading: "2. Eligibility",
      body: [
        "You must have the legal capacity to enter into these Terms and must comply with the laws and regulations applicable to you.",
        "You are responsible for determining whether cryptocurrency trading, automated trading, and the use of third-party cryptocurrency exchanges are lawful and appropriate in your jurisdiction.",
        "You must not use the Service for unlawful activities, fraud, market manipulation, money laundering, sanctions evasion, or other prohibited activities.",
      ],
    },
    {
      heading: "3. Exchange Accounts",
      body: [
        "The Service may connect to third-party cryptocurrency exchanges through API credentials supplied or authorized by the user.",
        "Your cryptocurrency assets remain within your exchange account unless otherwise expressly stated. Metabridge Academy does not become the owner of your cryptocurrency merely because you connect an exchange account to the Service.",
        "You remain responsible for maintaining your exchange account, complying with the exchange's terms, maintaining appropriate security settings, and ensuring that the API permissions you provide are appropriate.",
        "Where supported, users should create API credentials with only the permissions necessary for trading.",
        "Withdrawal permissions should not be enabled for API credentials supplied to the Service.",
        "The Service may reject credentials that present unacceptable permissions or security risks.",
      ],
    },
    {
      heading: "4. Testnet and Live Trading",
      body: [
        "The platform distinguishes between testnet/paper trading and live trading.",
        "Testnet or simulated trading does not involve the same financial consequences as live trading and does not guarantee equivalent results under real market conditions.",
        "Payment for the Service does not automatically authorize live trading.",
        "Live trading requires separate user authorization and appropriate production exchange credentials.",
        "When you deliberately enable live trading, you acknowledge that the Service may submit actual orders to your connected exchange account and that those orders may involve real financial gains or losses.",
      ],
    },
    {
      heading: "5. Automated Trading",
      body: [
        "Once properly configured and activated, the Service may perform trading operations automatically without requesting confirmation before every individual transaction.",
        "By enabling live automated trading, you expressly authorize the Service to submit supported trading instructions through the API credentials you have provided, subject to the settings and functionality available within the platform.",
        "You are responsible for monitoring your account and disabling automated trading when you no longer wish the system to operate.",
      ],
    },
    {
      heading: "6. Trading Risk",
      body: [
        "Cryptocurrency trading involves substantial financial risk.",
        "Prices may change rapidly, markets may become illiquid, exchanges may experience outages, orders may execute differently from expected, and trading strategies may perform differently under changing market conditions.",
        "Automated systems may also experience software errors, connectivity failures, delayed market data, exchange API failures, infrastructure outages, unexpected market behaviour, or other technical problems.",
        "You should never trade funds that you cannot afford to lose.",
        "The separate Trading Risk Disclosure forms part of these Terms.",
      ],
    },
    {
      heading: "7. No Guaranteed Returns",
      body: [
        "Metabridge Academy does not guarantee:",
        {
          list: [
            "profits or positive investment returns;",
            "prevention of trading losses;",
            "continuous availability of trading opportunities;",
            "execution at a particular price;",
            "uninterrupted exchange connectivity;",
            "uninterrupted operation of the Service; or",
            "future performance based on historical, simulated, testnet, or previous trading results.",
          ],
        },
        "Any performance information presented through the Service should be understood in its appropriate context and must not be interpreted as a guarantee of future performance.",
      ],
    },
    {
      heading: "8. User Responsibilities",
      body: [
        "You are responsible for:",
        {
          list: [
            "protecting your login credentials;",
            "securing your email and Telegram accounts;",
            "protecting your exchange account;",
            "selecting appropriate exchange API permissions;",
            "reviewing your trading configuration;",
            "monitoring your account and positions;",
            "understanding the risks associated with Spot and Futures trading;",
            "ensuring sufficient balances where applicable; and",
            "complying with applicable laws and third-party exchange requirements.",
          ],
        },
        "You must promptly revoke exchange API credentials if you believe they have been compromised.",
      ],
    },
    {
      heading: "9. Futures and Leveraged Trading",
      body: [
        "Futures and leveraged cryptocurrency trading can create significantly greater risk than ordinary Spot trading.",
        "Depending on the exchange, instrument, leverage, market conditions, and position, users may experience rapid losses, liquidation, or loss of a substantial portion of the funds committed to trading.",
        "Users who enable Futures functionality accept these additional risks.",
      ],
    },
    {
      heading: "10. Third-Party Services",
      body: [
        "The Service may depend on third parties, including cryptocurrency exchanges, payment processors, hosting infrastructure, messaging platforms, market-data providers, and internet-service providers.",
        "Metabridge Academy does not control the availability, security, policies, performance, or continued operation of independent third-party services.",
        "Failure or interruption of a third-party service may affect the operation of the trading bot.",
      ],
    },
    {
      heading: "11. Fees and Payments",
      body: [
        "Certain functionality may require payment.",
        "Prices and the features associated with paid access will be displayed before payment or otherwise communicated through the Service.",
        "Payment for access to the Service is separate from funds held in your cryptocurrency exchange account.",
        "Metabridge Academy does not require users to transfer their trading capital to the platform merely to connect the trading bot to a supported exchange.",
      ],
    },
    {
      heading: "12. Security",
      body: [
        "Metabridge Academy takes reasonable technical measures intended to protect sensitive information handled by the Service.",
        "However, no internet-connected system can guarantee absolute security.",
        "Users must take reasonable precautions to protect their own accounts, devices, credentials, exchange accounts, and communication channels.",
      ],
    },
    {
      heading: "13. Suspension and Termination",
      body: [
        "Metabridge Academy may restrict or suspend access where reasonably necessary to protect the Service, other users, infrastructure, or third parties, including suspected abuse, security threats, unlawful use, attempted circumvention of security controls, or material violations of these Terms.",
        "Users may stop using the Service and may revoke exchange API access directly through their exchange.",
      ],
    },
    {
      heading: "14. Service Changes",
      body: [
        "The Service may evolve over time. Features, supported exchanges, trading pairs, strategies, interfaces, and technical functionality may be added, modified, restricted, or discontinued.",
        "Where reasonably practicable, material changes affecting users will be communicated appropriately.",
      ],
    },
    {
      heading: "15. Limitation of Liability",
      body: [
        "To the extent permitted by applicable law, Metabridge Academy will not be responsible for losses arising solely from ordinary cryptocurrency market movements, a user's own exchange configuration or decisions, third-party exchange failures, or circumstances reasonably outside the platform's control.",
        "Nothing in these Terms excludes or limits liability that cannot lawfully be excluded or limited.",
      ],
    },
    {
      heading: "16. Intellectual Property",
      body: [
        "The software, interface, branding, documentation, algorithms, content, and other proprietary materials associated with the Service remain the property of their respective owners and may not be copied, reverse-engineered, redistributed, or commercially exploited except as permitted by law or written authorization.",
      ],
    },
    {
      heading: "17. Changes to These Terms",
      body: [
        "These Terms may be updated as the Service, regulatory environment, or operational requirements evolve.",
        "Material changes may require users to review or accept updated Terms before continuing to use affected functionality.",
      ],
    },
    {
      heading: "18. Contact",
      body: [
        "Questions concerning these Terms may be directed to:",
        "Metabridge Academy",
        "Website: metabridgeacademy.com",
        "Email: info@metabridgeacademy.com",
      ],
    },
  ],
};

export const RISK_DISCLOSURE = {
  title: "Trading Risk Disclosure",
  effectiveDate: "18 October 2026",
  intro: [
    "This Trading Risk Disclosure describes important risks associated with cryptocurrency and automated trading through the Metabridge Academy Trading Bot.",
    "Please read it carefully before enabling live trading.",
  ],
  sections: [
    {
      heading: "Cryptocurrency Trading Involves Risk",
      body: [
        "Cryptocurrency markets can be extremely volatile.",
        "The value of a cryptocurrency may rise or fall substantially within a short period. Trading may result in partial or substantial loss of capital.",
        "You should only trade with funds whose loss you can financially tolerate.",
      ],
    },
    {
      heading: "Automated Trading Risk",
      body: [
        "The Metabridge Academy Trading Bot may automatically submit orders according to its configured strategy and account settings.",
        "Automated trading introduces risks including:",
        {
          list: [
            "unexpected market behaviour;",
            "exchange API failures;",
            "network interruption;",
            "delayed or unavailable market data;",
            "software or infrastructure failures;",
            "unexpected order execution;",
            "insufficient liquidity;",
            "slippage;",
            "exchange downtime;",
            "strategy underperformance; and",
            "differences between expected and actual execution.",
          ],
        },
        "Automation does not eliminate trading risk.",
      ],
    },
    {
      heading: "No Guaranteed Profit",
      body: [
        "No trading strategy can guarantee profit.",
        "Past results, simulations, backtests, testnet activity, previous trades, demonstrations, educational materials, or historical performance do not guarantee future results.",
        "The absence of previous losses does not mean future losses cannot occur.",
      ],
    },
    {
      heading: "Testnet Results",
      body: [
        "Testnet trading is provided to allow users to learn and evaluate functionality without intentionally placing real-money orders.",
        "Testnet performance may differ materially from live-market performance.",
        "Successful testnet trading does not establish that the same strategy will be profitable when using real funds.",
      ],
    },
    {
      heading: "Spot Trading",
      body: [
        "Spot cryptocurrency trading can result in financial losses if the value of purchased assets declines.",
        "Market volatility, liquidity, exchange conditions, and other factors may materially affect outcomes.",
      ],
    },
    {
      heading: "Futures and Leverage",
      body: [
        "Futures trading involves additional risk.",
        "Leverage can amplify both gains and losses. Adverse market movements can result in rapid losses and, depending on exchange rules and account configuration, liquidation.",
        "Users should understand Futures trading before enabling it.",
      ],
    },
    {
      heading: "Exchange Risk",
      body: [
        "Trading operations rely on third-party exchanges.",
        "An exchange may experience downtime, API interruption, delayed execution, account restrictions, cybersecurity incidents, liquidity problems, regulatory intervention, or other events outside Metabridge Academy's control.",
      ],
    },
    {
      heading: "Technology Risk",
      body: [
        "No software system is infallible.",
        "Although reasonable measures may be used to improve reliability and security, technical failures may occur.",
        "Users should independently monitor their exchange accounts and should know how to disable the bot or revoke API access directly from their exchange when necessary.",
      ],
    },
    {
      heading: "API Security",
      body: [
        "Users should grant only the exchange API permissions necessary for supported trading operations.",
        "Do not enable cryptocurrency withdrawal permissions for API credentials connected to the Metabridge Academy Trading Bot.",
        "If you suspect that an API credential has been compromised, revoke it immediately through the exchange.",
      ],
    },
    {
      heading: "User Authorization",
      body: [
        "Enabling live trading is a deliberate action.",
        "When you enable live trading, you acknowledge that:",
        "real orders may be submitted to your cryptocurrency exchange account using real funds and financial losses may occur.",
        "Payment for access to the platform does not itself constitute authorization to trade real funds.",
      ],
    },
    {
      heading: "User Responsibility",
      body: [
        "You remain responsible for deciding whether automated cryptocurrency trading is appropriate for you and for monitoring your connected exchange account.",
        "By enabling live trading, you acknowledge that you understand and accept the risks described in this disclosure.",
      ],
    },
  ],
};

export const PRIVACY_POLICY = {
  title: "Privacy Policy",
  effectiveDate: "18 October 2026",
  intro: [
    "Metabridge Academy respects the privacy and security of users of its cryptocurrency trading platform.",
    "This Privacy Policy explains the categories of information the Service may process, why that information is used, and the measures applied to protect sensitive information.",
  ],
  sections: [
    {
      heading: "Information We Process",
      body: [
        "Depending on how you use the Service, information processed may include:",
        {
          list: [
            "email address;",
            "account identifiers;",
            "bot/account code;",
            "authentication and session information;",
            "trading-account configuration;",
            "selected trading pair;",
            "Spot or Futures selection;",
            "risk settings;",
            "payment status and transaction references;",
            "Telegram linkage information;",
            "trading history and performance information;",
            "operational logs and error information;",
            "exchange API credentials supplied by the user; and",
            "other technical information necessary to operate and secure the Service.",
          ],
        },
      ],
    },
    {
      heading: "Exchange API Credentials",
      body: [
        "Exchange API credentials are sensitive information.",
        "Where the Service stores exchange credentials, appropriate technical safeguards, including encryption, are used to protect them.",
        "API secrets are not intended to be displayed through ordinary customer or administrative interfaces after storage.",
        "Users should provide only the exchange permissions necessary for supported trading functionality and should not enable withdrawal permissions.",
      ],
    },
    {
      heading: "How Information Is Used",
      body: [
        "Information may be processed to:",
        {
          list: [
            "create and maintain user accounts;",
            "authenticate users;",
            "operate automated trading functionality;",
            "connect authorized exchange accounts;",
            "execute user-authorized trading functionality;",
            "provide Telegram notifications;",
            "process and verify payments;",
            "display account and trading information;",
            "diagnose errors;",
            "protect the platform against abuse and security threats;",
            "maintain service reliability;",
            "provide customer support; and",
            "comply with applicable legal obligations.",
          ],
        },
      ],
    },
    {
      heading: "Payment Information",
      body: [
        "Payments may be processed through third-party payment providers such as Paystack.",
        "Metabridge Academy may receive transaction references, payment status, amount, customer identifiers, and other information required to verify payment.",
        "Sensitive card details are handled according to the payment provider's systems and are not intended to be stored by the Metabridge Academy Trading Bot itself.",
      ],
    },
    {
      heading: "Telegram",
      body: [
        "Users may optionally link Telegram to receive bot notifications.",
        "Telegram's processing of information through its own service is subject to Telegram's own terms and privacy practices.",
      ],
    },
    {
      heading: "Cryptocurrency Exchanges",
      body: [
        "When users connect an exchange account, information is transmitted to or received from that exchange as necessary to provide the requested trading functionality.",
        "The exchange independently processes information according to its own terms, policies, and infrastructure.",
      ],
    },
    {
      heading: "Security",
      body: [
        "Metabridge Academy uses reasonable administrative and technical safeguards intended to protect information against unauthorized access, disclosure, alteration, or destruction.",
        "Measures may include encryption of sensitive exchange credentials, restricted server access, authentication controls, and other security practices.",
        "No internet-connected service can guarantee absolute security.",
      ],
    },
    {
      heading: "Data Retention",
      body: [
        "Information may be retained for as long as reasonably necessary to operate the Service, maintain security and transaction records, satisfy legitimate operational requirements, resolve disputes, and meet applicable legal obligations.",
        "Retention periods may vary according to the category of information and the reason it is maintained.",
      ],
    },
    {
      heading: "User Responsibilities",
      body: [
        "Users are responsible for maintaining the security of their login credentials, devices, email accounts, Telegram accounts, and cryptocurrency exchange accounts.",
        "Users should immediately revoke exchange API credentials if compromise is suspected.",
      ],
    },
    {
      heading: "Third-Party Services",
      body: [
        "The Service relies on independent third-party providers, potentially including payment processors, cryptocurrency exchanges, Telegram, infrastructure providers, and other technology services.",
        "Those providers may process information under their own privacy policies and legal obligations.",
      ],
    },
    {
      heading: "Privacy Requests",
      body: [
        "Subject to applicable law and legitimate retention requirements, users may contact Metabridge Academy regarding questions or requests concerning personal information associated with their account.",
      ],
    },
    {
      heading: "Changes to This Policy",
      body: [
        "This Privacy Policy may be updated to reflect changes in the Service, technology, operational practices, or applicable legal requirements.",
        "Material changes may be communicated through the Service where appropriate.",
      ],
    },
    {
      heading: "Contact",
      body: [
        "Privacy questions may be directed to:",
        "Metabridge Academy",
        "Website: metabridgeacademy.com",
        "Email: info@metabridgeacademy.com",
      ],
    },
  ],
};
