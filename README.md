Small pfSense utility written in Python32 that will print Wifi Vouchers to a esc/POS thermal printer.

WifiVouchers reqires that you "pre-load" the application with voucher codes from a CSV file generated in the Captive Portal. Go to PfSense webGUI/Captive Portal and create Wifi Voucher rolls then choose menu option to export those rolls to CSV file and then copy over the csv file into the same directory as WifiVouchers.

Launch WifiVouchers and simply click on Print Voucher. A messagebox will prompt you to confirm the voucher printed successfully (in case of paper jam etc.).
Clicking YES will delete the voucher number from the CSV file and next time a new voucher number will be printed. Clicking CANCEL will not delete the voucher from the csv file and instead allow you to print the last voucher number again.

Pre-requisites: The ini file must be present in the current directory. The name of the printer in the ini file must match the name of the esc/POS printer listed in the Windows Printer & scanners list. The https_address is the IP address or resolvable DNS name of your pfSense firewall (e.g. 192.168.1.1 or pfsense.mycafe.com), the captive portal zone_name, the URL_scheme (either http or https), and finally after successful verification the auto redirect to a website of your choice. Be aware that if your URL_scheme is set to HTTPS then you should not use self-signed certificates otherwise the guest will receive certificate errors.

Additional Notes:
Each voucher roll in pfSense can have a maximum of 65,535 codes. Creating 10 rolls in pfSense captive portal and then joining those rolls into one large csv file could potentially mean 0.7m codes pre-loaded into the app before requiring new roll generation. Be aware that pre-loading 0.7m voucher codes would slow the app considerably.
