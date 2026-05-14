#!/usr/bin/env node

/**
 * Lyft Tutorial Batch Extractor CLI - Fixed Version
 * Properly handles lesson extraction after starting tutorial
 */

const puppeteer = require('puppeteer');
const fs = require('fs').promises;
const path = require('path');

var cname = '';
// Configuration
const config = {
    baseUrl: 'https://www-staging.lyft.net',
    outputDir: './extracted_tutorials',
    credentials: {
        phone: '',
        email: 'qa@lyft.com',
        verificationCode: '123456',
        driverLicenseCode: '1234',
        driverLicenseCodeAlt: '0000',
        userName: ''
    },
    courses: [],
    languages: ['en', 'es', 'fr', 'pt'],
    headless: false,
    captureScreenshots: false,
    darkMode: false
};

// Parse command line arguments
const args = process.argv.slice(2);
let argIndex = 0;

while (argIndex < args.length) {
    const arg = args[argIndex];
    
    switch(arg) {
        case '--name':
            cname = args[++argIndex];
            console.log('coursename', cname);
            break;
        case '--phone':
            config.credentials.phone = args[++argIndex];
            break;
        case '--email':
            config.credentials.email = args[++argIndex];
            break;
        case '--vcode':
            config.credentials.verificationCode = args[++argIndex];
            break;
        case '--dlcode':
            config.credentials.driverLicenseCode = args[++argIndex];
            break;
        case '--dlcode-alt':
        case '--dlalt':
            config.credentials.driverLicenseCodeAlt = args[++argIndex];
            break;
        case '--username':
        case '--user':
            config.credentials.userName = args[++argIndex];
            break;
        case '--course':
            config.courses.push(args[++argIndex]);
            break;
        case '--courses-file':
            const coursesFile = args[++argIndex];
            try {
                const content = fs.readFileSync(coursesFile, 'utf8');
                config.courses = content.split('\n').filter(line => line.trim() && !line.startsWith('#'));
            } catch (e) {
                console.error(`Failed to read courses file: ${coursesFile}`);
                process.exit(1);
            }
            break;
        case '--languages':
            config.languages = args[++argIndex].split(',');
            break;
        case '--headless':
            config.headless = true;
            break;
        case '--screenshots':
            config.captureScreenshots = true;
            break;
        case '--dark-mode':
            config.darkMode = true;
            break;
        case '--output':
            config.outputDir = args[++argIndex];
            break;
        case '--debug':
            process.env.DEBUG = 'true';
            config.headless = false;
            break;
        case '--force':
            config.forceExtraction = true;
            break;
        case '--help':
            showHelp();
            process.exit(0);
            break;
        default:
            console.error(`Unknown argument: ${arg}`);
            showHelp();
            process.exit(1);
    }
    argIndex++;
}

function showHelp() {
    console.log(`
Lyft Tutorial Batch Extractor CLI
==================================

Usage: node audit-lessons-fix.js [options]

Required Options:
  --phone <number>        Phone number for login (REQUIRED)
  --course <id>           Course ID to extract (REQUIRED)

Optional:
  --name <name>           Custom name for the course output
  --languages <list>      Languages to extract (default: en,es,fr,pt)
  --debug                 Enable debug mode with screenshots
  --output <dir>          Output directory (default: ./extracted_tutorials)

Example:
  node audit-lessons-fix.js --phone 4305558360 --course 2yQq04tUUk1H67xlZA7PLn --name "Your first ride demo"
`);
}

// Validate configuration
if (!config.credentials.phone) {
    console.error('Error: Phone number is required');
    showHelp();
    process.exit(1);
}

if (config.courses.length === 0) {
    console.error('Error: At least one course must be specified');
    showHelp();
    process.exit(1);
}

// Utility functions
function log(message, level = 'info') {
    const timestamp = new Date().toISOString();
    const prefix = {
        info: '✅',
        warn: '⚠️',
        error: '❌',
        debug: '🔍'
    }[level] || '📍';
    
    console.log(`[${timestamp}] ${prefix} ${message}`);
}

async function ensureDirectory(dirPath) {
    try {
        await fs.mkdir(dirPath, { recursive: true });
    } catch (error) {
        log(`Failed to create directory: ${dirPath}`, 'error');
        throw error;
    }
}

async function saveJSON(data, filePath) {
    try {
        await fs.writeFile(filePath, JSON.stringify(data, null, 2));
        log(`Saved: ${filePath}`);
    } catch (error) {
        log(`Failed to save ${filePath}: ${error.message}`, 'error');
    }
}

async function saveText(text, filePath) {
    try {
        await fs.writeFile(filePath, text);
        log(`Saved: ${filePath}`);
    } catch (error) {
        log(`Failed to save ${filePath}: ${error.message}`, 'error');
    }
}

// Browser automation class
class TutorialExtractor {
    
    constructor(browser, page) {
        this.browser = browser;
        this.page = page;
        this.authenticated = false;
        this.debugMode = process.env.DEBUG === 'true';
    }

    async authenticate() {
        log('Starting authentication...', 'info');
        
        // Navigate to logout first
        await this.page.goto(`${config.baseUrl}/logout`, { waitUntil: 'networkidle2' });
        await this.wait(1000);
        
        // Navigate to tutorial page
        const testUrl = `${config.baseUrl}/learningcenter/tutorial/${config.courses[0]}`;
        log(`Navigating to: ${testUrl}`, 'debug');
        await this.page.goto(testUrl, { waitUntil: 'networkidle2' });
        await this.wait(2000);
        
        // Authentication loop
        let authComplete = false;
        let attempts = 0;
        const maxAttempts = 10;
        
        while (!authComplete && attempts < maxAttempts) {
            attempts++;
            await this.wait(1000);
            
            // Check for Terms of Service
            try {
                const tosModal = await this.page.$('[data-testid="terms-scrollwrap-modal"]');
                if (tosModal) {
                    log('Terms of Service detected', 'info');
                    const agreeBtn = await this.page.$('button[data-testid="terms-scrollwrap-modal-button"]');
                    if (agreeBtn) {
                        await this.wait(1000);
                        await agreeBtn.click();
                        await this.wait(2000);
                    }
                }
            } catch (e) {}
            
            // Get page header
            let headerText = '';
            try {
                const pageHeader = await this.page.$('h1[data-testid="page-header"]');
                if (pageHeader) {
                    headerText = await pageHeader.evaluate(el => el.textContent);
                    log(`Page: "${headerText}"`, 'debug');
                }
            } catch (e) {}
            
            // Phone number page
            if (headerText.includes('Welcome back to Lyft')) {
                log('Phone number page', 'info');
                const phoneInput = await this.page.$('input[name="phone"]');
                if (phoneInput) {
                    await this.fillField(phoneInput, config.credentials.phone);
                    const submitBtn = await this.page.$('button[data-testid="formSubmit"]');
                    if (submitBtn) {
                        await submitBtn.click();
                        await this.waitForPageChange();
                    }
                }
            }
            
            // SMS verification
            else if (headerText.includes('Enter verification code')) {
                log('SMS verification', 'info');
                const codeInput = await this.page.$('input[name="phoneCode"]');
                if (codeInput) {
                    await this.fillField(codeInput, config.credentials.verificationCode);
                    const nextBtn = await this.page.$('button[data-testid="form-submit"]');
                    if (nextBtn) {
                        await nextBtn.click();
                        await this.waitForPageChange();
                    }
                }
            }
            
            // User verification
            else if (headerText.includes('Is that you')) {
                log('User verification', 'info');
                const yesButton = await this.page.$('button[data-aid="challenge"]');
                if (yesButton) {
                    await yesButton.click();
                    await this.waitForPageChange();
                }
            }
            
            // Identity verification
            else if (headerText.includes('Verify identity')) {
                log('Identity verification', 'info');
                const challengeInput = await this.page.$('input[data-testid="challenge-input-field"]');
                if (challengeInput) {
                    const inputName = await challengeInput.evaluate(el => el.getAttribute('name'));
                    if (inputName === 'drivers_license_number') {
                        await this.fillField(challengeInput, config.credentials.driverLicenseCode);
                    } else {
                        await this.fillField(challengeInput, config.credentials.email);
                    }
                    const nextBtn = await this.page.$('button[data-testid="form-submit"]');
                    if (nextBtn) {
                        await nextBtn.click();
                        await this.waitForPageChange();
                    }
                }
            }
            
            // Check for success
            const currentUrl = this.page.url();
            if (currentUrl.includes('/tutorial/') || currentUrl.includes('/lesson/')) {
                const hasContent = await this.page.evaluate(() => {
                    return !!document.getElementById('__NEXT_DATA__');
                });
                
                if (hasContent) {
                    authComplete = true;
                    this.authenticated = true;
                    log('Authentication successful!', 'info');
                    break;
                }
            }
            
            await this.wait(1000);
        }
        
        if (!authComplete) {
            log('Authentication failed', 'error');
            if (!config.forceExtraction) {
                throw new Error('Authentication failed');
            }
        }
    }

    async fillField(element, value) {
        await element.click();
        await this.wait(100);
        await element.focus();
        await this.wait(100);
        
        // Clear field
        await element.click({ clickCount: 3 });
        await this.wait(100);
        for (let i = 0; i < 20; i++) {
            await this.page.keyboard.press('Backspace');
        }
        
        // Type value
        for (const char of value) {
            await this.page.keyboard.type(char, { delay: 80 + Math.random() * 40 });
        }
        
        await this.wait(200);
    }

    async waitForPageChange(timeout = 5000) {
        try {
            await Promise.race([
                this.page.waitForNavigation({ waitUntil: 'networkidle2', timeout }),
                this.wait(timeout)
            ]);
        } catch (e) {}
    }

    async extractCourse(courseId, languages) {
        log(`Starting extraction for course: ${courseId}`, 'info');
        const courseData = {};
        
        for (const lang of languages) {
            log(`\n=== Processing language: ${lang} ===`, 'info');
            
            // Navigate to course with specific language
            const url = `${config.baseUrl}/learningcenter/tutorial/${courseId}?locale_language=${lang}`;
            log(`Navigating to: ${url}`, 'debug');
            
            try {
                await this.page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
                await this.wait(3000); // Wait for content to load
                
                // Check if we need to click Start button
                const startBtn = await this.page.$('button[data-testid="start-btn"]');
                if (startBtn) {
                    log('Clicking Start button to begin tutorial...', 'info');
                    await startBtn.click();
                    await this.wait(3000); // Wait for first lesson to load
                }
                
                // Now extract the build ID and lesson data
                log('Extracting course metadata...', 'debug');
                const extractedData = await this.page.evaluate(() => {
                    const data = {
                        buildId: null,
                        courseId: null,
                        courseName: null,
                        lessons: [],
                        currentLesson: null,
                        error: null
                    };
                    
                    try {
                        const nextData = document.getElementById('__NEXT_DATA__');
                        if (nextData) {
                            const parsed = JSON.parse(nextData.textContent);
                            data.buildId = parsed.buildId;
                            
                            // Try to extract from different possible locations
                            const queries = parsed?.props?.pageProps?.dehydratedState?.queries || [];
                            
                            for (const query of queries) {
                                // Check for course data
                                const course = query?.state?.data?.course;
                                if (course) {
                                    data.courseId = course.courseContainerId;
                                    data.courseName = course.name;
                                    
                                    // Extract lesson metadata
                                    if (course.lessonMetadata && Array.isArray(course.lessonMetadata)) {
                                        data.lessons = course.lessonMetadata.map(lesson => ({
                                            id: lesson.lessonId,
                                            name: lesson.name || 'Unnamed',
                                            status: lesson.lessonStatus,
                                            type: lesson.lessonType
                                        }));
                                    }
                                }
                                
                                // Check for current lesson data (when on a lesson page)
                                const lesson = query?.state?.data?.lesson;
                                if (lesson) {
                                    data.currentLesson = {
                                        id: lesson.lessonId,
                                        name: lesson.name,
                                        activities: lesson.activities
                                    };
                                }
                            }
                            
                            // Alternative extraction from page props
                            if (data.lessons.length === 0) {
                                const pageProps = parsed?.props?.pageProps;
                                if (pageProps?.course) {
                                    data.courseId = pageProps.course.courseContainerId;
                                    data.courseName = pageProps.course.name;
                                    
                                    if (pageProps.course.lessonMetadata) {
                                        data.lessons = pageProps.course.lessonMetadata.map(lesson => ({
                                            id: lesson.lessonId,
                                            name: lesson.name || 'Unnamed',
                                            status: lesson.lessonStatus,
                                            type: lesson.lessonType
                                        }));
                                    }
                                }
                            }
                        }
                    } catch (e) {
                        data.error = e.message;
                    }
                    
                    return data;
                });
                
                if (extractedData.error) {
                    log(`Extraction error: ${extractedData.error}`, 'error');
                }
                
                // If we still don't have lessons, try to navigate through the tutorial
                if (extractedData.lessons.length === 0 && extractedData.currentLesson) {
                    log('No lesson list found, extracting from current lesson...', 'warn');
                    // We'll need to navigate through each lesson manually
                    extractedData.lessons = await this.extractLessonsByNavigation(courseId, lang);
                }
                
                log(`Found build ID: ${extractedData.buildId}`, 'info');
                log(`Found ${extractedData.lessons.length} lessons`, 'info');
                
                courseData[lang] = {
                    ...extractedData,
                    locale: lang,
                    extractedAt: new Date().toISOString(),
                    url: url,
                    authenticated: this.authenticated
                };
                
                // Fetch lesson content via API if we have the necessary data
                if (extractedData.buildId && extractedData.lessons.length > 0) {
                    log(`Fetching content for ${extractedData.lessons.length} lessons...`, 'info');
                    courseData[lang].lessonContent = await this.fetchLessonsContent(
                        extractedData.buildId,
                        courseId,
                        extractedData.lessons,
                        lang
                    );
                    
                    const successCount = courseData[lang].lessonContent.filter(l => l.content).length;
                    log(`Successfully fetched ${successCount}/${extractedData.lessons.length} lessons`, 'info');
                } else {
                    log(`No lessons to fetch for ${lang}`, 'warn');
                }
                
            } catch (error) {
                log(`Failed to process language ${lang}: ${error.message}`, 'error');
                courseData[lang] = {
                    error: error.message,
                    locale: lang,
                    extractedAt: new Date().toISOString()
                };
            }
            
            // Wait between languages to avoid rate limiting
            await this.wait(2000);
        }
        
        return courseData;
    }

    async extractLessonsByNavigation(courseId, lang) {
        log('Attempting to extract lessons by navigation...', 'info');
        const lessons = [];
        let lessonCount = 0;
        const maxLessons = 50; // Safety limit
        
        while (lessonCount < maxLessons) {
            // Extract current lesson data
            const lessonData = await this.page.evaluate(() => {
                const nextData = document.getElementById('__NEXT_DATA__');
                if (!nextData) return null;
                
                try {
                    const parsed = JSON.parse(nextData.textContent);
                    const queries = parsed?.props?.pageProps?.dehydratedState?.queries || [];
                    
                    for (const query of queries) {
                        const lesson = query?.state?.data?.lesson;
                        if (lesson) {
                            return {
                                id: lesson.lessonId,
                                name: lesson.name || `Lesson ${lessonCount + 1}`,
                                status: lesson.lessonStatus,
                                type: lesson.lessonType
                            };
                        }
                    }
                } catch (e) {
                    return null;
                }
                return null;
            });
            
            if (lessonData && !lessons.find(l => l.id === lessonData.id)) {
                lessons.push(lessonData);
                log(`Found lesson: ${lessonData.name}`, 'debug');
            }
            
            // Try to find and click next button
            const nextBtn = await this.page.$('button[data-testid="next-btn"], button[data-testid="continue-btn"]');
            if (nextBtn) {
                await nextBtn.click();
                await this.wait(2000);
                lessonCount++;
            } else {
                // No more lessons
                break;
            }
        }
        
        return lessons;
    }

    async fetchLessonsContent(buildId, courseId, lessons, locale) {
        const contents = [];
        
        for (let i = 0; i < lessons.length; i++) {
            const lesson = lessons[i];
            log(`Fetching lesson ${i + 1}/${lessons.length}: ${lesson.name} (${locale})`, 'debug');
            
            const apiUrl = `${config.baseUrl}/learningcenter/_next/data/${buildId}/lesson/${courseId}/${lesson.id}.json`;
            const params = new URLSearchParams({
                courseContainerId: courseId,
                lessonId: lesson.id,
                locale_language: locale
            });
            
            const fullUrl = `${apiUrl}?${params.toString()}`;
            
            try {
                const content = await this.page.evaluate(async (url) => {
                    try {
                        const response = await fetch(url, {
                            credentials: 'include',
                            headers: {
                                'Accept': 'application/json',
                            }
                        });
                        
                        if (!response.ok) {
                            return { 
                                error: `HTTP ${response.status}: ${response.statusText}`,
                                status: response.status 
                            };
                        }
                        
                        const data = await response.json();
                        return data;
                    } catch (e) {
                        return { error: e.message };
                    }
                }, fullUrl);
                
                if (content && !content.error) {
                    contents.push({
                        ...lesson,
                        content: content
                    });
                    log(`✅ Fetched: ${lesson.name}`, 'debug');
                } else {
                    const errorMsg = content?.error || 'Unknown error';
                    log(`Failed to fetch lesson ${lesson.id}: ${errorMsg}`, 'error');
                    contents.push({
                        ...lesson,
                        content: null,
                        error: errorMsg
                    });
                }
            } catch (error) {
                log(`Failed to fetch lesson ${lesson.id}: ${error.message}`, 'error');
                contents.push({
                    ...lesson,
                    content: null,
                    error: error.message
                });
            }
            
            await this.wait(500);
        }
        
        return contents;
    }

    wait(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }
}

// Format data as text
function formatAsText(data) {
    let text = '';
    
    const isMultiLanguage = !data.courseId && !data.lessons && typeof data === 'object';
    
    if (isMultiLanguage) {
        const languages = Object.keys(data);
        const firstLang = data[languages[0]];
        
        text += `COMPLETE TUTORIAL EXTRACTION\n`;
        text += `${'='.repeat(80)}\n\n`;
        text += `Course ID: ${firstLang?.courseId || 'Unknown'}\n`;
        text += `Course Name: ${firstLang?.courseName || 'Unknown'}\n`;
        text += `Languages Available: ${languages.join(', ')}\n`;
        text += `Extracted: ${new Date().toISOString()}\n\n`;
        
        languages.forEach(lang => {
            const langData = data[lang];
            
            text += `\n${'='.repeat(80)}\n`;
            text += `LANGUAGE: ${lang.toUpperCase()}\n`;
            text += `${'='.repeat(80)}\n\n`;
            
            if (langData.error) {
                text += `ERROR: ${langData.error}\n\n`;
            } else {
                text += formatSingleLanguage(langData);
            }
        });
    } else {
        text += formatSingleLanguage(data);
    }
    
    return text;
}

function formatSingleLanguage(langData) {
    let text = '';
    
    // Add header for single language extraction
    text += `TUTORIAL EXTRACTION\n`;
    text += `${'='.repeat(60)}\n`;
    text += `Course ID: ${langData.courseId || 'Unknown'}\n`;
    text += `Course Name: ${langData.courseName || 'Unknown'}\n`;
    text += `Language: ${langData.locale || 'Unknown'}\n`;
    text += `Build ID: ${langData.buildId || 'Unknown'}\n`;
    text += `Extracted: ${langData.extractedAt || new Date().toISOString()}\n\n`;
    
    if (langData.lessons && langData.lessons.length > 0) {
        text += `LESSONS OVERVIEW (${langData.lessons.length} total)\n`;
        text += `${'-'.repeat(40)}\n`;
        
        langData.lessons.forEach((lesson, idx) => {
            text += `${idx + 1}. ${lesson.name || 'Unnamed Lesson'}\n`;
            text += `   ID: ${lesson.id}\n`;
            text += `   Status: ${lesson.status || 'N/A'}\n`;
            text += `   Type: ${lesson.type || 'N/A'}\n`;
        });
        text += '\n';
    }
    
    const lessonsArray = langData.lessonContent || [];
    
    if (lessonsArray.length > 0) {
        text += `\nDETAILED LESSON CONTENT\n`;
        text += `${'-'.repeat(60)}\n`;
        
        lessonsArray.forEach((lesson, lessonIdx) => {
            text += `\n### LESSON ${lessonIdx + 1}: ${lesson.name || 'Unnamed Lesson'}\n`;
            text += `ID: ${lesson.id}\n\n`;
            
            let activities = [];
            
            if (lesson.content) {
                if (lesson.content.pageProps?.dehydratedState?.queries?.[0]?.state?.data?.lesson?.activities) {
                    activities = lesson.content.pageProps.dehydratedState.queries[0].state.data.lesson.activities;
                }
            }
            
            if (activities.length > 0) {
                activities.forEach((activity, actIdx) => {
                    text += `\n#### Activity ${actIdx + 1}: ${activity.title || 'Untitled Activity'}\n\n`;
                    
                    if (activity.components && Array.isArray(activity.components)) {
                        activity.components.forEach(component => {
                            if (component.text?.paragraphs) {
                                component.text.paragraphs.forEach(para => {
                                    text += `${para}\n\n`;
                                });
                            }
                            
                            if (component.list) {
                                if (component.list.title) {
                                    text += `**${component.list.title}**\n`;
                                }
                                const items = component.list.items || [];
                                items.forEach((item, idx) => {
                                    text += `  ${idx + 1}. ${item}\n`;
                                });
                                text += '\n';
                            }
                            
                            if (component.video) {
                                text += `[VIDEO: ${component.video.alt || 'Video content'}]\n\n`;
                            }
                            
                            if (component.image) {
                                text += `[IMAGE: ${component.image.alt || 'Image'}]\n\n`;
                            }
                        });
                    }
                });
            }
            
            text += `\n${'-'.repeat(60)}\n`;
        });
    }
    
    return text;
}

// Main execution
async function main() {
    log('Starting Lyft Tutorial Batch Extractor', 'info');
    log(`Courses to extract: ${config.courses.join(', ')}`, 'info');
    log(`Languages: ${config.languages.join(', ')}`, 'info');
    
    await ensureDirectory(config.outputDir);
    
    const browser = await puppeteer.launch({
        headless: false,
        executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        args: [
            "--user-data-dir=/Users/ramiroabelardodelgado/Desktop/testTextExtractor/chrome-profile-copy",
            '--disable-blink-features=AutomationControlled',
            '--disable-gpu',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--window-size=1920,1080',
            '--start-maximized'
        ],
        ignoreDefaultArgs: ['--enable-automation'],
        defaultViewport: null
    });

    console.log('Browser launched');
    
    try {
        const page = await browser.newPage();
        await page.setViewport({ width: 1920, height: 1080 });
        await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
        
        const extractor = new TutorialExtractor(browser, page);
        
        // Authenticate once
        await extractor.authenticate();
        
        // Process each course
        for (const courseId of config.courses) {
            log(`\n📚 Processing course: ${courseId}`, 'info');
            
            try {
                const courseData = await extractor.extractCourse(courseId, config.languages);
                
                const courseName = cname || courseData[config.languages[0]]?.courseName || courseId;
                const courseDir = path.join(config.outputDir, courseName);
                await ensureDirectory(courseDir);
                
                // Save complete multi-language JSON
                await saveJSON(courseData, path.join(courseDir, `${courseName}_all_languages.json`));
                
                // Save individual language files with proper naming convention
                for (const [lang, langData] of Object.entries(courseData)) {
                    if (langData && !langData.error) {
                        // Format single language data
                        const singleLangText = formatSingleLanguage(langData);
                        
                        // English gets no suffix, other languages get _lang suffix
                        const filename = lang === 'en' 
                            ? `${courseName}_en.txt`
                            : `${courseName}_${lang}.txt`;
                        
                        await saveText(singleLangText, path.join(courseDir, filename));
                        
                        // Also save individual JSON files with same naming convention
                        const jsonFilename = lang === 'en'
                            ? `${courseName}_en.json`
                            : `${courseName}_${lang}.json`;
                        
                        await saveJSON(langData, path.join(courseDir, jsonFilename));
                    }
                }
                
                log(`✅ Course ${courseId} extracted successfully`, 'info');
                
            } catch (error) {
                log(`Failed to extract course ${courseId}: ${error.message}`, 'error');
            }
            
            await extractor.wait(2000);
        }
        
    } catch (error) {
        log(`Fatal error: ${error.message}`, 'error');
        console.error(error);
    } finally {
        await browser.close();
    }
    
    log('\n🎉 Extraction complete!', 'info');
    log(`Output saved to: ${path.resolve(config.outputDir)}`, 'info');
}

// Run the main function
main().catch(error => {
    console.error('Unhandled error:', error);
    process.exit(1);
});
