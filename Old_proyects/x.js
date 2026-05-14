//auditV4.js
const puppeteer = require('puppeteer');
const fs = require('fs');
const { buffer } = require('stream/consumers');
const { timeout } = require('puppeteer');
const { time } = require('console');
//initial vars
let contentful = process.argv[2];
let phone = process.argv[3];
let course = process.argv[4];
course = course.trim();
print('course: '+course);

if (process.argv[5] !== undefined && process.argv[5] !== null) {
    if (process.argv[5].includes(',')) {
        languages = process.argv[5].replace(/[\s\"\[\]]/g, '').split(',');
    } else {
        languages = [process.argv[5]];
    }

}else{
    languages = ['en','es','fr'];
}

const coursedir = `./${course}`;
    if (!fs.existsSync(coursedir)) {
        fs.mkdirSync(coursedir);
    }

print('languages: '+languages);

let email = "qa@lyft.com";
let vcode = "123456";
let vcode2 ="1234"
let debugging = true;
let chk_darkmode = false;

let previoustext = '';


print('course: '+course);

if (process.argv[6] !== undefined && process.argv[6] !== null) {
    chk_darkmode = true;
}
//helper functions
let secondtime;
function isOverflowing(element) {
    return element.scrollHeight > element.clientHeight;
  }
async function screenshot(page,lang,ss) {
    print('Start screenshotfn()');

    const bodyText = await page.evaluate(() => document.body.innerText);
    if (bodyText === previoustext) {
        print('no change, skip screenshot');
        return;
    }else{
       /*  try {
            resize = await resize(page);
        } catch (error) {
            print('error  @ resize', error);
        } */
        //scrollHeight = 852;
        try {
            // try RESIZE
            scrollHeight = await page.evaluate(() => {
                const element = document.evaluate('/html/body/div/div/div/div/div/main/div[1]/div/div/div[2]/div[1]', document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;

                if (element) {
                    try {
                        console.log('skipping class removal');
                        //element.removeAttribute('class');
                    } catch (e) {
                        console.log('error removing class attribute', e);
                    }
                    element.setAttribute('height', '100%');
                    element.setAttribute('overflow-y', 'visible');

                }
                return element ? element.scrollHeight : 0;
            }, { timeout: 5000 }); // Adding a timeout of 5000ms
            //await new Promise(resolve => setTimeout(resolve, 5000000 )); // Pause for 99999 minutes


            console.log('Element scroll height:', scrollHeight);

        } catch (error) {
            console.log('error  setting height attrinute to 100% @ div', error);
        };
        
        try {
            await page.evaluate(() => {
                const myelement = document.evaluate('/html/body/div/div/div/div/div/main/div[1]/div/div/div[2]/div[1]', document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;

                if (myelement) {
                    myelement.setAttribute('style', 'position: relative; display:flex; flex-direction: column; padding:  4px 12px 4px 16px;' );
                }
                return myelement;
        
            });
            console.log('DONE CHANGING STYLE')
            /* if (secondtime) {
                await new Promise(resolve => setTimeout(resolve, 5000000 )); // Pause for 99999 minutes             
            };
            secondtime = true;
 */            
        } catch (error) {
            console.log('error  setting STYLE attrinute to 100% @ div', error);
        }
        

        previoustext = bodyText;
        let bodyHeight = await page.evaluate(() => document.documentElement.scrollHeight);
        let totalHeight = await page.evaluate(() => {
            return document.documentElement.scrollHeight;
        });
        console.log('Total HTML height:', totalHeight);

        //const bodyWidth = await page.evaluate(() => document.documentElement.clientWidth);
        const bodyWidth = 400;
        
        console.log(`BODY WIDTH: ${bodyWidth}, BODY HEIGHT: ${bodyHeight}`);

        let myclip = {
            x: 0,
            y: 0,
            width: bodyWidth,
            height: bodyHeight,
        };

        console.log(bodyText);
        ss_f = String(ss++).padStart(2, '0');

        const langFolder = lang.toUpperCase();
        fs.mkdirSync(langFolder, { recursive: true });

        console.log('screenshotfn()_'+ss_f);
        body = await page.$('body');
        try {
            await body.screenshot({
                path: `./${course}/${langFolder}/${lang}_screenshot_${ss_f}.png`,
                clip: myclip,
                //fullPage: true,
            });
        } catch (error) {
            console.log('error  taking screenshot', error);
        }
        
        
        try {
            fs.writeFileSync(`./${course}/${langFolder}/${lang}_screenshot_${ss_f}.txt`, bodyText);
        } catch (error) {
            console.log('error  writing text', error);
        }
        return;
    };
 
  /*   body = await page.$('body');

    const contentHeight = await page.evaluate(() => {
        return document.body.scrollHeight;
    });

    print(contentHeight)
    if (contentHeight > 852) {
        await page.setViewport({ width: 393, height: contentHeight });
    } */

    //console.log(`content height: ${contentHeight}`);
    
    //element = await page.$('body');
    //await page.setViewport({ width: 393, height: 852 });
};
async function resize(page) {
    scrollHeight = await page.evaluate(() => {
        const element = document.evaluate('/html/body/div/div/div/div/div/main/div[1]/div/div/div[2]/div[1]', document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (element) {
            element.setAttribute('height', '100%');
        }
        const scrollHeight = element ? element.scrollHeight : 0;
        console.log('Element scroll height:', scrollHeight);
        return scrollHeight;

    });
    return scrollHeight;
}
const dataTestIds = {
    'phone-input': { action: 'fill', value: phone },
    'verification-code-field': { action: 'fill', value: vcode },
    'formSubmit': { action: 'click' },
    'challenge': { action: 'click' },
    'email': { action: 'fill', value: email },
    'form-submit': { action: 'click' },
    'core-ui-button': { action: 'click' },
    'start-btn': { action: 'click' },
    'next-btn': { action: 'click' },
    'video': { action: 'video' },
    'dark-mode-btn': { action: 'dark-mode-btn' },
    'challenge-input-field': { action: 'fill' },
};

async function performAction(page, dataTestId) {
    const actionData = dataTestIds[dataTestId];
    const timeout = 5000; // Set a default timeout for actions

    if (dataTestId == 'email') {
        await page.locator('input[name="email"][data-testid="challenge-input-field"]').fill(actionData.value, { timeout });
    } else if (dataTestId == 'start-btn') {
        await page.locator('button[data-testid="start-btn"]').click({ timeout });
        print('button data-testid=start-btn clicked');

    } else if (actionData) {
        if (actionData.action === 'fill') {
            await page.locator(`input[data-testid="${dataTestId}"]`).fill(actionData.value, { timeout });
        } else if (actionData.action === 'click') {
            try {
                await page.waitForSelector(`button[data-testid="${dataTestId}"]`, { timeout });
                await page.locator(`button[data-testid="${dataTestId}"]`).click({ timeout });
                console.log(`button data-testid=${dataTestId} clicked`);

            } catch (error) {
                console.log(`button data-testid=${dataTestId} not found`);
            }
        }

        if (actionData.action === 'video') {
            print('video found');
        }
        if (actionData.action === 'dark-mode-btn') {
            await page.locator(`input[data-testid="${dataTestId}"]`).click({ timeout });
        }
    }
}

function print(substep){
    console.log('----->>'+substep);
}

async function toggleDarkmode(page) {
    if (chk_darkmode == false) {      
        return;
    }else{
        print('toggle dark-mode-btn');
        await performAction(page, 'dark-mode-btn');
        await new Promise(r => setTimeout(r, 2000));
    };
    return;
}

async function login(page) {
    //login
    print('Page title:', await page.title());

    print('login');
  
    //phone
    console.log('phone');
    await page.locator('input[data-testid="phone-input"][name="phone"]');

    await performAction(page, 'phone-input');
    await performAction(page, 'formSubmit');
    
    await page.waitForNavigation();
    

    //EnterVerif_Code
    await new Promise(resolve => setTimeout(resolve, 1000));

    const verifCodeField = await page.locator('input[data-testid="verification-code-field"]');
    if (verifCodeField) {
        await performAction(page, 'verification-code-field');

    }else{
        print('no verif code');
    }
    await new Promise(resolve => setTimeout(resolve, 1000));


    try {
        const startBtn = await page.waitForSelector('button[data-testid="start-btn"]', { timeout: 1500 });
        if (startBtn) {
            print('Start button found, exiting');
            return;
        }

        
    } catch (error) {
        print('no start button, continue authentication');
        
    }

    //Driver License Challenge
    await new Promise(resolve => setTimeout(resolve, 1000));

    try {
        const driverLicenseChallenge = await page.waitForSelector('input[data-testid="challenge-input-field"]', { timeout: 2000 });
        if (driverLicenseChallenge) {
            print('Entered Driver License Challenge');
            input = page.locator('input[data-testid="challenge-input-field"]');
            await page.type('input[data-testid="challenge-input-field"]', vcode2, { delay: 100 });
            await new Promise(resolve => setTimeout(resolve, 1000));
            page.locator('button[data-testid="form-submit"]').click();
            //await performAction(page, 'challenge-input-field', vcode2);
            //await performAction(page, 'form-submit');
            print('Driver License Challenge submitted');     }
    } catch (error) {
        print('no driver license challenge, continue authentication');
        print(error);
    }

    try {
        const startBtn = await page.waitForSelector('button[data-testid="start-btn"]', { timeout: 1000 });
        if (startBtn) {
            print('Start button found, exiting');
            return;
        }

        
    } catch (error) {
        print('no start button, continue authentication');
        
    }
    
    //Are you the driver?
    print('Are you the driver?');
    const coreUiButton = await page.locator('button[data-testid="core-ui-button"]', { timeout: 1500 });
    if (coreUiButton) {
        print('Are you the driver challenge');
        await performAction(page, 'core-ui-button');
    }
    try {
        const startBtn = await page.waitForSelector('button[data-testid="start-btn"]', { timeout: 1500 });
        if (startBtn) {
            print('Start button found, exiting');
            return;
        }

        
    } catch (error) {
        print('no start button, continue authentication');
        
    }

    //Email Challenge
    print('Email Challenge');
    const emailchallenge = await page.locator('input[name="email"][data-testid="challenge-input-field"]');
    if (emailchallenge) {
        //add email and submit
        await performAction(page, 'email');
        await performAction(page, 'form-submit');
        print('Email submitted');
    }
    await page.waitForNavigation();
    const startBtn = await page.waitForSelector('button[data-testid="start-btn"]', { timeout: 2000 });
    if (startBtn) {
        print('Start button found, exiting');
        return;
    }
    return
};

async function playvideo(page) {
    const timeout = 500; // Set a default timeout for playvideo actions
    let video;
    try {
        // try play video
        video = await page.locator('video[data-testid="video"]', { timeout });
        clicked = await video.click({ timeout }); // Use await here
    if (clicked !== null && clicked !== undefined) {
        await new Promise(resolve => setTimeout(resolve, 5000));
    }

    } catch (error) {
        print('no video found');
    }
    return
}
async function browseTutorial(page,lang,ss) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const timeout = 500; // Set a default timeout for browseTutorial actions

    
        print('browseTutorial_FN');
      


        

        //
        // 
        

        // toggle dark-mode-btn
        //await toggleDarkmode(page);

        // toggle dark-mode-btn
        //await toggleDarkmode(page);

        
        buttons = [
            'button[data-testid="start-btn"]',
            'button[data-testid="core-ui-button"]',
            'button[data-testid="next-btn"]',            
        ];

        let counter = 0;
        while (counter < buttons.length) {
            try {

                
                // Wait for the button to be visible and clickable
                await page.waitForSelector(buttons[counter], { visible: true, timeout: 1000 }); 
                const button = await page.$(buttons[counter],{timeout: 1000});
                if (button && button !== null) {
                    print('button found');
                    try {
                        // try screenshot
                        await screenshot(page,lang,ss,{timeout: 1500});
                        lesson = true;
                    } catch (error) {
                        print( 'error screenshot @ screenshot', error);
                    }
                    
                    try{
                        await playvideo(pages),{timeout: 1000};
                    }
                    catch(error){
                        print('error @ playvideo', error);
                    }

                    await button.click();
                    return;
                    counter = 100;
                }else{
                    print('found but couldnt click button');
                    counter++;
                };
            } catch (error) {
                print('could not find button ${counter}', error);
                counter++;
                };
            }

            if (counter == buttons.length) {
                print('no buttons found, exiting');
                lesson = false;
                return;
            };
            
            

        await new Promise(resolve => setTimeout(resolve, 1000));
        
};

async function run() {

    console.log('run');
    const browser = await puppeteer.launch({
        headless: false,
        args: ["--user-data-dir=/Users/ramiroabelardodelgado/Library/Application Support/Google/Chrome/"],
        executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        ignoreDefaultArgs: ["--enable-automation"]
    });
    lang = 'en';
    const page = await browser.newPage();
    const url = `https://www-staging.lyft.net/learningcenter/tutorial/${contentful}?locale_language=${lang}`;
    const logoutUrl = `https://www-staging.lyft.net/logout`;
    
    await page.goto(logoutUrl);

    await page.waitForNavigation();
    
    print(logoutUrl)

    await page.goto(url);
    await page.setViewport({ width: 393, height: 852 });
    
    await login(page);

    //Inicia Tutorial
   /*  const startBtn = page.locator('button[data-testid="start-btn"]');
    if (startBtn) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        print('Start button found');
        //await screenshot(page, {timeout: 5000});
        startBtn.click({timeout: 500});
        await new Promise(resolve => setTimeout(resolve, 500));
        //await performAction(page, 'start-btn');
    } */

    for (const lang of languages) {
        console.log(lang);

        const dir = `./${course}/${lang}/`;
        if (!fs.existsSync(dir)) {
            fs.mkdirSync(dir);
        }
    
        let ss=0;


        lesson = true;
        
        let langurl = `https://www-staging.lyft.net/learningcenter/tutorial/${contentful}?locale_language=${lang}`;

        await page.goto(langurl);
        while (lesson){
        //await new Promise(resolve => setTimeout(resolve, 10000000))
        // ;
            console.log('stillon the loop',ss++);
            await browseTutorial(page,lang,ss);
    
        };
        print(`finished lesson for ${lang}`);
    }
    process.exit(0);
    
}

run();

